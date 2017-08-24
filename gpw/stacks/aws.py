#!/usr/bin/env python

from __future__ import print_function
from six.moves import input
from six.moves.urllib.parse import urlparse
import time
import yaml

from botocore.exceptions import ClientError

import gpw.stacks
import gpw.utils


class CloudformationStack(gpw.stacks.BaseStack):
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
            template_body = gpw.utils.get_template_body(template_url)

            if ".mako" in template_url.path[-5:]:
                if not hasattr(self, "Parameters"):
                    self.Parameters = {}
                self.Parameters["build_id"] = self.BuildId
                args = [self.StackName, template_body, self.Parameters]
                template = gpw.utils.parse_mako(*args)
                # mako doesn't need Parameters as they're available to the
                # template as python variables
                del self.Parameters
            elif ".jinja" in template_url.path[-6:]:
                args = [self.StackName, template_body, self.Parameters]
                template = gpw.utils.parse_jinja(*args)
                # jinja doesn't need Parameters as they're available to the
                # template as python variables
                del self.Parameters
            elif ".json" in template_url.path[-5:]:
                args = [self.StackName, template_body, self.Parameters]
                template = gpw.utils.parse_json(*args)
            elif ".yaml" in template_url[-5:]:
                args = [self.StackName, template_body, self.Parameters]
                template = gpw.utils.parse_yaml(*args)
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
        gpw.utils.BOTO_CF_RESOURCE.create_stack(**self.__dict__)
        if wait:
            waiter = gpw.utils.BOTO_CF_CLIENT.get_waiter(
                "stack_create_complete"
            )
            waiter.wait(StackName=self.StackName)

    def delete(self, wait=False):
        cf_stack = gpw.utils.BOTO_CF_RESOURCE.Stack(self.StackName)
        cf_stack.delete()
        if wait:
            waiter = gpw.utils.BOTO_CF_CLIENT.get_waiter(
                "stack_delete_complete"
            )
            waiter.wait(StackName=self.StackName)

    def update(self, wait=False, review=True):
        self.validate()
        if review:
            self.manage_change_set()
        else:
            cf_stack = gpw.utils.BOTO_CF_RESOURCE.Stack(self.StackName)
            cf_stack.update(**self.__dict__)
        if wait:
            waiter = gpw.utils.BOTO_CF_CLIENT.get_waiter(
                "stack_update_complete"
            )
            waiter.wait(StackName=self.StackName)

    def manage_change_set(self, wait=False):
        # find build ID in tags
        for tag in self.Tags:
            if tag["Key"] == "build_id":
                build_id = tag["Value"]
        change_set_name = "{}-{}".format(self.StackName, build_id)

        gpw.utils.BOTO_CF_CLIENT.create_change_set(
            ChangeSetName=change_set_name,
            ChangeSetType="UPDATE",
            **self.__dict__
        )

        # wait for change set to be ready
        time.sleep(2)
        waiter = gpw.utils.BOTO_CF_CLIENT.get_waiter(
            "change_set_create_complete"
        )
        waiter.wait(ChangeSetName=change_set_name, StackName=self.StackName)

        change_set = gpw.utils.BOTO_CF_CLIENT.describe_change_set(
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
            waiter = gpw.utils.BOTO_CF_CLIENT.get_waiter(
                "stack_update_complete"
            )
            waiter.wait(StackName=self.StackName)

    def changeset_user_input(self, change_set_name):
        answer = input("Execute(e), Delete (d), or Keep(k) change set? ")
        if answer == "e":
            print("Executing changeset {}...".format(change_set_name))
            gpw.utils.BOTO_CF_CLIENT.execute_change_set(
                ChangeSetName=change_set_name,
                StackName=self.StackName
            )
        elif answer == "d":
            print("Deleting changeset {}. No changes made to stack {}".format(change_set_name, self.StackName)) # noqa
            gpw.utils.BOTO_CF_CLIENT.delete_change_set(
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
            gpw.utils.BOTO_CF_CLIENT.validate_template(
                TemplateBody=self.TemplateBody
            )
        except ClientError as exc:
            raise SystemExit(exc.response["Error"]["Message"])