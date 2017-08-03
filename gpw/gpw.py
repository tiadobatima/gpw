#!/usr/bin/env python

from __future__ import print_function
from apiclient.discovery import build
from apiclient.errors import HttpError
import argparse
import jinja2
import logging
import os
import requests
from six.moves import input
from six.moves.urllib.parse import parse_qs
from six.moves.urllib.parse import urlparse
from six.moves.urllib.parse import urlunparse
import subprocess
import time
import yaml

import boto3
from botocore.exceptions import ClientError
import jmespath
import mako.exceptions
import mako.template


# CFN API objects
BOTO_CF_RESOURCE = boto3.resource('cloudformation')
BOTO_CF_CLIENT = boto3.client('cloudformation')
STACK_CACHE = {}
CF_STACK_RESOURCE_CACHE = {}


# GCP API objects
GCP_API = build("deploymentmanager", "v2")
GCP_STACK_CACHE = {}


def yaml_cloudformation_constructor(loader, node):
    """ Implements the yaml tag !Cloudformation

    The tag takes a dict {stack: $stack_name, output: output_key}
    as node (argument).

    Example:
      VpcId: !Cloudformation {stack: ${vpc_stack}, output: VPC}
      VpcId: !Cloudformation {stack: ${vpc_stack}, resource_id: VPC}
    """
    output_dict = loader.construct_mapping(node)
    stack_name = output_dict["stack"]
    if "output" in output_dict.keys():
        return get_stack_output(stack_name, output_dict["output"])
    elif "resource_id" in output_dict.keys():
        return get_stack_resource(stack_name, output_dict["resource_id"])
    else:
        raise SystemExit("Either 'output' or 'resource_id' must be provided")


def yaml_aws_constructor(loader, node):
    """ Implements the yaml tag !AWS

    The tag takes a dict {service: $service_name,
      action: $action_name, arguments: $arguments,
      result_filter: $jmespath_string}
    as node (argument)

    Example:
      VpcId: !AWS {service: ec2, action: describe_vpcs, arguments: {Filters: [{Name: "tag:team", Values: [sometag]}]}, result_filter: "Vpcs[].VpcId"}
    """
    args_dict = loader.construct_mapping(node, deep=True)
    return call_aws(**args_dict)


def yaml_gcp_dm_constructor(loader, node):
    """ Implements the yaml tag !GCPDM

    The tag takes a dict {deployment: $stack_name, output: output_key}
    as node (argument).

    Example:
      VpcId: !GCPDM {deployment: ${vpc_stack}, output: VPC}
    """
    output_dict = loader.construct_mapping(node)
    if "output" in output_dict.keys():
        return get_stack_output(
            stack_name=output_dict["deployment"],
            output_key=output_dict["output"],
            provider="gcp",
            project=output_dict["project"]
        )
    else:
        raise SystemExit("Either 'output' or 'resource' must be provided")


yaml.add_constructor(u'!Cloudformation', yaml_cloudformation_constructor)
yaml.add_constructor(u'!AWS', yaml_aws_constructor)
yaml.add_constructor(u'!GCPDM', yaml_gcp_dm_constructor)


def get_stack_output(
        stack_name,
        output_key,
        provider="cloudformation",
        **kwargs):
    if provider == "cloudformation":
        # caching results of calls to clouformation API
        if not STACK_CACHE.get(stack_name):
            STACK_CACHE[stack_name] = BOTO_CF_RESOURCE.Stack(stack_name)

        for output in STACK_CACHE[stack_name].outputs:
            if output["OutputKey"] == output_key:
                return output["OutputValue"]
    elif provider == "gcp":
        if not STACK_CACHE.get(stack_name):
            deployment = GCP_API.deployments().get(
                project=kwargs["project"],
                deployment=stack_name
            ).execute()
            manifest = GCP_API.manifests().get(
                project=kwargs["project"],
                deployment=stack_name,
                manifest=deployment["manifest"].split("/")[-1]
                ).execute()
            STACK_CACHE[stack_name] = {
                "deployment": deployment,
                "manifest": manifest
            }
        layout = yaml.load(STACK_CACHE[stack_name]["manifest"]["layout"])
        for output in layout.get("outputs", []):
            if output["name"] == output_key:
                return output["finalValue"]
    return ""


def get_stack_resource(stack_name, resource_id):
    # caching results of calls to clouformation API
    if not CF_STACK_RESOURCE_CACHE.get(stack_name):
        CF_STACK_RESOURCE_CACHE[stack_name] = {}
    if not CF_STACK_RESOURCE_CACHE[stack_name].get(resource_id):
        CF_STACK_RESOURCE_CACHE[stack_name][resource_id] = \
            BOTO_CF_RESOURCE.StackResource(stack_name, resource_id)
    return CF_STACK_RESOURCE_CACHE[stack_name][resource_id].physical_resource_id # noqa


def call_aws(service, action, arguments={}, result_filter=None):
    client = boto3.client(service)
    result = getattr(client, action)(**arguments)
    if result_filter is None:
        return result
    return jmespath.search(result_filter, result)


class Stack(object):
    """ Base class for different types of stacks.
    """
    def __init__(self, **kwargs):
        [setattr(self, k, v) for k, v in kwargs.items()]

    @staticmethod
    def factory(**kwargs):
        # default type is Cloudformation
        possible_stack_type_keys = ["StackType", "stack_type", "Type", "type"]
        stack_keys = kwargs.keys()
        for possible_stack_type in possible_stack_type_keys:
            if possible_stack_type in stack_keys:
                stack_type = kwargs.pop(possible_stack_type).lower()
                break
        else:
            stack_type = "cloudformation"

        if stack_type == "cloudformation":
            return CloudformationStack(**kwargs)
        elif stack_type == "shell":
            return ShellStack(**kwargs)
        elif stack_type == "gcp":
            return GCPStack(**kwargs)
        raise SystemExit("Stack type not supported: {}".format(stack_type))


class CloudformationStack(Stack):
    def __init__(self, **kwargs):
        """
        Args:
            All Cloudformation supported sections are allowed, plus:
            - BuildId(str): The build ID. This will be merged to the
              parameters dict.

        All arguments provided will be set as object attributes, but
        the attributes not supported by CNF will be unset after
        initialization so the attributes can be fed to the CNF API
        wholesale.
        """
        super(CloudformationStack, self).__init__(**kwargs)

        if isinstance(self.TemplateBody, dict):
            self.TemplateBody = yaml.safe_dump(self.TemplateBody, indent=2)
        else:
            template_url = urlparse(self.TemplateBody)
            template_body = get_template_body(template_url)

            if ".mako" in template_url.path[-5:]:
                if not hasattr(self, "Parameters"):
                    self.Parameters = {}
                self.Parameters["build_id"] = self.BuildId
                args = [self.StackName, template_body, self.Parameters]
                template = parse_mako(*args)
                # mako doesn't need Parameters as they're available to the
                # template as python variables
                del self.Parameters
            elif ".jinja" in template_url.path[-6:]:
                args = [self.StackName, template_body, self.Parameters]
                template = parse_jinja(*args)
                # jinja doesn't need Parameters as they're available to the
                # template as python variables
                del self.Parameters
            elif ".json" in template_url.path[-5:]:
                args = [self.StackName, template_body, self.Parameters]
                template = parse_json(*args)
            elif ".yaml" in template_url[-5:]:
                args = [self.StackName, template_body, self.Parameters]
                template = parse_yaml(*args)
            else:
                raise SystemExit("file extension not supported")

            self.TemplateBody = yaml.safe_dump(template, indent=2)

        # make sure "Tags" is a list of dicts. Making a shallow copy
        # just in case
        self.Tags = getattr(self, "Tags", {})
        tags = self.Tags.copy()
        if isinstance(tags, dict):
            self.Tags = [{"Key": k, "Value": v} for k, v in tags.items()]
        self.Tags.append({"Key": "build_id", "Value": self.BuildId})

        # cleanup non-cfn attributes
        del self.BuildId

    def create(self, wait=False):
        self.validate()
        BOTO_CF_RESOURCE.create_stack(**self.__dict__)
        if wait:
            waiter = BOTO_CF_CLIENT.get_waiter('stack_create_complete')
            waiter.wait(StackName=self.StackName)

    def delete(self, wait=False):
        cf_stack = BOTO_CF_RESOURCE.Stack(self.StackName)
        cf_stack.delete()
        if wait:
            waiter = BOTO_CF_CLIENT.get_waiter('stack_delete_complete')
            waiter.wait(StackName=self.StackName)

    def update(self, wait=False, review=True):
        self.validate()
        if review:
            self.manage_change_set()
        else:
            cf_stack = BOTO_CF_RESOURCE.Stack(self.StackName)
            cf_stack.update(**self.__dict__)
        if wait:
            waiter = BOTO_CF_CLIENT.get_waiter("stack_update_complete")
            waiter.wait(StackName=self.StackName)

    def manage_change_set(self, wait=False):
        # find build ID in tags
        for tag in self.Tags:
            if tag["Key"] == "build_id":
                build_id = tag["Value"]
        change_set_name = "{}-{}".format(self.StackName, build_id)

        BOTO_CF_CLIENT.create_change_set(
            ChangeSetName=change_set_name,
            ChangeSetType="UPDATE",
            **self.__dict__
        )

        # wait for change set to be ready
        time.sleep(2)
        waiter = BOTO_CF_CLIENT.get_waiter("change_set_create_complete")
        waiter.wait(ChangeSetName=change_set_name, StackName=self.StackName)

        change_set = BOTO_CF_CLIENT.describe_change_set(
            ChangeSetName=change_set_name,
            StackName=self.StackName
        )
        change_set.pop("ResponseMetadata")
        print("---------- Change Set ----------")
        print(yaml.safe_dump(change_set, indent=2))
        print("--------------------------------")

        answer = False
        while not answer:
            answer = self.changeset_user_input(change_set_name)

        if wait:
            waiter = BOTO_CF_CLIENT.get_waiter('stack_update_complete')
            waiter.wait(StackName=self.StackName)

    def changeset_user_input(self, change_set_name):
        answer = input("Execute(e), Delete (d), or Keep(k) change set? ")
        if answer == "e":
            print("Executing changeset {}...".format(change_set_name))
            BOTO_CF_CLIENT.execute_change_set(
                ChangeSetName=change_set_name,
                StackName=self.StackName
            )
        elif answer == "d":
            print("Deleting changeset {}. No changes made to stack {}".format(change_set_name, self.StackName)) # noqa
            BOTO_CF_CLIENT.delete_change_set(
                ChangeSetName=change_set_name,
                StackName=self.StackName
            )
        elif answer == "k":
            print("Changeset {} unchanged. No changes made to stack {}".format(change_set_name, self.StackName)) # noqa
        else:
            print("Valid answers: e, d, k")
            return False
        return True

    def upsert(self, wait=False):
        self.validate()
        try:
            self.update(wait=wait)
        except ClientError as exc:
            if "does not exist" in exc.response["Error"]["Message"]:
                self.create(wait=wait)
            else:
                raise

    def render(self):
        # un-stringfy the TemplateBody so it displays nicely on screen
        template = self.__dict__.copy()
        template["TemplateBody"] = yaml.load(template["TemplateBody"])
        print(yaml.safe_dump(template, indent=2))

    def validate(self):
        try:
            BOTO_CF_CLIENT.validate_template(TemplateBody=self.TemplateBody)
        except ClientError as exc:
            raise SystemExit(exc.response["Error"]["Message"])


class ShellStack(Stack):
    """ Class for stacks of type "Shell"

    This class allows for running commands in the local system.
    Mostly used to provide a consistent interface to infrastructure
    deployments when resources cannot be handled using
    cloudformation
    """
    def __init__(self, **kwargs):
        """
        Args:
            Actions(dict): Actions allowed in for the stack. For each
                action, these dict keys are available:
                - Commands(str|list): Required. Represents the shell
                commands to be executed for the action, and works
                similarly to the "args" option in "subprocess.Popen()"
                - Environments(dict): Optional. Represents environment
                variables specific to the action
            BuildId(str): The build ID. It will be exported as an
                environment BUILD_ID.
            Shell(str): The shell do be used. Defaults to system shell,
                which is /bin/sh in most linux systems.
                If "Commands" is a list, this variable has no effect as
                the commands are not executed inside a shell
            Environment(dict): Stack-wide environment variables. These
                variables will be set in all actions, unless overridden
                by action-specific variables.

        Example Stack:
            StackType: Shell
            Shell: /bin/bash
            Environment:
              AWS_DEFAULT_REGION: us-west-2
            Actions:
              Create:
                Environment:
                  KMS_KEY: !Cloudformation {stack: kms-stack, output: key_arn}
                Commands: |
                  cmd1
                  cmd2
              Delete:
                Commands: cmd3
        """
        super(ShellStack, self).__init__(**kwargs)

        self.Shell = getattr(self, "Shell", "/bin/bash")
        self.Environment = getattr(self, "Environment", {})

        # Expands shell variables if command is a string
        for k, v in self.Actions.items():
            if isinstance(v["Commands"], str):
                self.Actions[k]["Commands"] = os.path.expandvars(v["Commands"])

    def _execute(self, action):
        """ Executes local commands in the system
        """
        if action not in self.Actions.keys():
            raise SystemExit("Action not available: {}".format(action))

        action_params = self.Actions[action]

        commands = action_params.get("Commands")
        if not commands:
            raise SystemExit(
                "At least one command must be specified in a shell stack"
            )

        if isinstance(commands, str):
            args = {"shell": True, "executable": self.Shell or None}
        elif isinstance(commands, list):
            args = {}
        else:
            raise SystemExit(
                "commands must be non a empty list or str: {}".format(commands)
            )
        # Merge global and action specific environment variables.
        # Action specific variables win.
        environment = dict(os.environ.copy(), **self.Environment)
        environment.update(action_params.get("Environment", {}))
        environment["BUILD_ID"] = self.BuildId

        process = subprocess.Popen(commands, env=environment, **args)
        process.wait()
        if process.returncode:
            logging.error(
                "Command {} exited with return code {}".format(
                    commands,
                    process.returncode
                )
            )
            raise SystemExit(process.returncode)

    def create(self, wait=False):
        self._execute(action="Create")

    def delete(self, wait=False):
        self._execute(action="Delete")

    def update(self, wait=False, review=False):
        self._execute(action="Update")

    def render(self, wait=False):
        print(yaml.dump(self.Actions, indent=2))


class GCPStack(Stack):

    GCP_DEPLOYMENT_BODY_KEYS = [
        "description",
        "fingerprint",
        "labels",
        "name",
        "target"
    ]

    def __init__(self, **kwargs):
        """
        Args:
            All GCP DM supported sections are allowed, plus:
            - BuildId(str): The build ID. This will be merged to the
              parameters dict.
            - project(str): The project the deployment is going to be created
              under

        All arguments provided will be set as object attributes.
        Unlike Clouformation class, the attributes of the object will not be
        used directly to feed the actions, as we need to massage the data quite
        a bit in GCP
        """
        super(GCPStack, self).__init__(**kwargs)

        # make sure "local" is a list of dicts. Making a shallow copy
        # just in case
        self.labels = getattr(self, "labels", {})
        labels = self.labels.copy()
        if isinstance(labels, dict):
            self.labels = [{"key": k, "value": v} for k, v in labels.items()]
        self.labels.append({"key": "build_id", "value": self.BuildId})
        self.target = self.assemble_target()
        self.body = self.assemble_body()

    def assemble_target(self):
        """ Assembles the target argument for DM's resource representation

        In GCP, the arguments mapping provided in a config file don't follow
        the DM's API, so we have to reorder the arguments before feeding them
        to the API.

        """
        # build imports
        imports = []
        for i in getattr(self, "imports", []):
            with open(i["path"]) as f:
                content = f.read().rstrip()
            imports.append(
                {
                    "content": content,
                    "name": i.get("name", i["path"])
                }
            )

        # build config
        config = {}
        for k, v in self.__dict__.items():
            if k in ["imports", "resources", "outputs"]:
                config[k] = v
        return {
            "imports": imports,
            "config": {
                "content": yaml.dump(
                    config,
                    indent=2,
                    default_flow_style=False
                )
            }
        }

    def assemble_body(self):
        """ Assembles the target argument for DM's resource representation

        In GCP, the arguments mapping provided in a config file don't follow
        the DM's API, so we have to reorder the arguments before feeding them
        to the API.

        """
        body = {}
        for k, v in self.__dict__.items():
            if k in self.GCP_DEPLOYMENT_BODY_KEYS:
                body[k] = v
        return body

    def get(self):
        """ Gets the deployment data.

        This method returns an empty dict instead of a 404 exception raised by
        the GCP SDK.
        """

        try:
            return GCP_API.deployments().get(
                project=self.project,
                deployment=self.name
            ).execute()
        except HttpError as exc:
            if exc.resp["status"] == "404":
                return {}
            raise SystemExit(
                "HTTP error {}: {}".format(exc.resp["status"], exc.content)
            )

    def wait(self, interval=5, timeout=300):
        """ A waiter for stack completeness

        GCP SDK doesn't provide a waiter, so improvising a quick on here.

        Args:
            interval(int): Interval between probes in seconds
            timeout(int): The total wait timeout in seconds

        """
        n_probes = int(timeout/interval)
        for i in range(0, n_probes):
            time.sleep(interval)
            deployment = self.get()
            if deployment and deployment["operation"]["status"] == "DONE":
                break

    def create(self, wait=False):
        GCP_API.deployments().insert(
            project=self.project,
            body=self.body
        ).execute()
        if wait:
            self.wait()

    def delete(self, wait=False):
        if not self.get():
            raise SystemExit("Deployment doesn't exist: {}".format(self.name))
        GCP_API.deployments().delete(
            project=self.project,
            deployment=self.name
        ).execute()
        if wait:
            self.wait()

    def update(self, wait=False, review=False):
        GCP_API.deployments().insert(
            project=self.project,
            body=self.body
        ).execute()
        if wait:
            self.wait()

    def upsert(self, wait=False):
        if self.get():
            self.update(wait=wait)
        else:
            self.create(wait=wait)

    def render(self):
        deployment = {"project": self.project, "body": self.body}
        print(yaml.safe_dump(deployment, indent=2))

    def validate(self):
        pass


def get_template_body(url):
    """ Returns the text of the URL

    Args:
        url(str): a RFC 1808 compliant URL

    Returns: The text of the target URL

    This function supports 3 different schemes:
        - http/https
        - s3
        - path
    """
    if "http" in url.scheme:
        return requests.get(urlunparse(url)).text
    elif "s3" in url.scheme:
        s3_client = boto3.client("s3")
        extra_args = {k: v[0] for k, v in parse_qs(url.query).items()}
        obj = s3_client.get_object(
            Bucket=url.netloc,
            Key=url.path[1:],
            **extra_args
        )
        return obj["Body"].read()
    return open(url.path).read()


def parse_mako(stack_name, template_body, parameters):

    """ Parses Mako templates
    """
    # The default for strict_undefined is False. Change to True to
    # troubleshoot pesky templates
    mako_template = mako.template.Template(
        template_body,
        strict_undefined=False
    )
    parameters["get_stack_output"] = get_stack_output
    parameters["get_stack_resource"] = get_stack_resource
    parameters["call_aws"] = call_aws
    try:
        template = yaml.load(mako_template.render(**parameters))
    except:
        raise SystemExit(
            mako.exceptions.text_error_template().render()
        )

    # Automatically adds and merges outputs for every resource in the
    # template - outputs are automatically exported.
    # An existing output in the template will not be overriden by an
    # automatic output.
    outputs = {
        k: {"Value": {"Ref": k}, "Export": {"Name": "{}-{}".format(stack_name, k)}} for k in template.get("Resources", {}).keys()  # noqa
    }
    outputs.update(template.get("Outputs", {}))
    template["Outputs"] = outputs
    return template


def parse_jinja(stack_name, template_body, parameters):
    jinja_template = jinja2.Template(template_body)
    parameters["get_stack_output"] = get_stack_output
    parameters["get_stack_resource"] = get_stack_resource
    parameters["call_aws"] = call_aws
    template = yaml.load(jinja_template.render(**parameters))

    # Automatically adds and merges outputs for every resource in the
    # template - outputs are automatically exported.
    # An existing output in the template will not be overriden by an
    # automatic output.
    outputs = {
        k: {"Value": {"Ref": k}, "Export": {"Name": "{}-{}".format(stack_name, k)}} for k in template.get("Resources", {}).keys()  # noqa
    }
    outputs.update(template.get("Outputs", {}))
    template["Outputs"] = outputs
    return template


def parse_json(stack_name, template_body, parameters):
    raise SystemExit("json templates not yet supported")


def parse_yaml(stack_name, template_body, parameters):
    raise SystemExit("yaml templates not yet supported")


def main():
    parser = argparse.ArgumentParser("Manage cloudformation stacks")
    parser.add_argument(
        "action",
        type=str,
        choices=[
            "create",
            "delete",
            "list",
            "render",
            "update",
            "upsert",
            "validate"
        ],
        help="The action to be performed"
    )
    parser.add_argument(
        "stack",
        type=argparse.FileType("r"),
        help=("The path to the stack file. Use - for stdin, in which case -t must be specified") # noqa
    )
    parser.add_argument(
        "--templating-engine",
        "-t",
        type=str,
        choices=["mako", "jinja", "yaml"],
        default="mako",
        help="The templating engine to render the stack. Only used when stack comes from stdin (-)" # noqa
    )
    parser.add_argument(
        "--wait",
        "-w",
        action="store_true",
        default=False,
        help="Waits for the stack to be ready/deleted before exiting"
    )
    parser.add_argument(
        "--review",
        "-r",
        action="store_true",
        default=False,
        help="Review changes"
    )
    parser.add_argument(
        "--build-id",
        "-b",
        default=os.getenv("BUILD_ID", ""),
        help="The build id. Defaults to BUILD_ID env variable"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Not implemented yet"
    )
    parser.add_argument(
        "--loglevel",
        "-l",
        default="error",
        help="The log level"
    )
    parser.add_argument(
        "--botocore-loglevel",
        default="error",
        help="The log level for botocore"
    )

    args = parser.parse_args()

    if not args.build_id:
        raise SystemExit("The build ID is required. \
            Use -b option or set BUILD_ID")

    # logging
    loglevel = getattr(logging, args.loglevel.upper(), None)
    botocore_loglevel = getattr(logging, args.botocore_loglevel.upper(), None)

    # botocore logging level
    boto_logger = logging.getLogger("botocore")
    boto_logger.setLevel(level=botocore_loglevel)

    # script logging level
    logging.basicConfig(level=loglevel)

    # Figure out what templating engine to use.
    # Only use -t option when stack comes from stdin
    if args.stack.name == "<stdin>":
        templating_engine = args.templating_engine
    elif ".mako" in args.stack.name[-5:]:
        templating_engine = "mako"
    elif ".jinja" in args.stack.name[-6:]:
        templating_engine = "jinja"
    elif ".yaml" in args.stack.name[-5:]:
        templating_engine = "yaml"
    else:
        raise SystemExit("Set templating engine to 'mako', 'jinja', or ''. \
            Or use the appropriate file extension")

    stack_file = args.stack.read()
    template_params = {
        "build_id": args.build_id,
        "call_aws": call_aws,
        "get_stack_output": get_stack_output,
        "get_stack_resource": get_stack_resource
    }

    # try rendering stack with mako first, if fails try jinja,
    # so we get all the goodies on the stack level as well,
    # not just the on the template

    if templating_engine == "mako":
        logging.debug("Trying to render mako input file...")
        try:
            stack_template = mako.template.Template(
                stack_file,
                strict_undefined=False
            )
            stack_attributes = yaml.load(
                stack_template.render(**template_params)
            )
        # mako wraps the exception where the real information is, so we unwrap
        # and display only the part that matters to the user
        except mako.exceptions:
            raise SystemExit(mako.exceptions.text_error_template().render())
    elif templating_engine == "jinja":
        stack_template = jinja2.Template(stack_file)
        stack_attributes = yaml.load(stack_template.render(**template_params))
    else:
        stack_attributes = yaml.load(stack_file)

    stack_attributes["BuildId"] = args.build_id

    stack = Stack.factory(**stack_attributes)

    if args.action == "create":
        stack.create(wait=args.wait)
    if args.action == "delete":
        stack.delete(wait=args.wait)
    if args.action == "update":
        stack.update(wait=args.wait, review=args.review)
    if args.action == "upsert":
        stack.upsert(wait=args.wait)
    if args.action == "render":
        print("===> Stack Attributes:")
        print(yaml.dump(stack_attributes, indent=2))
        print("===> Final Template:")
        stack.render()
    if args.action == "list":
        pass
    if args.action == "validate":
        stack.validate()


if __name__ == "__main__":
    main()
