#!/usr/bin/env python

from __future__ import print_function
import argparse
import logging
import os
import yaml

import jinja2
import mako.exceptions
import mako.template

import gpw.utils
import gpw.stacks


def main():
    """ entry point

    TODO (gus): replace "action" option with subparser
    """
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
        "call_aws": gpw.utils.call_aws,
        "get_stack_output": gpw.utils.get_stack_output,
        "get_stack_resource": gpw.utils.get_stack_resource
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
        except:
            raise SystemExit(mako.exceptions.text_error_template().render())
    elif templating_engine == "jinja":
        stack_template = jinja2.Template(stack_file)
        stack_attributes = yaml.load(stack_template.render(**template_params))
    else:
        stack_attributes = yaml.load(stack_file)

    stack_attributes["BuildId"] = args.build_id

    stack = gpw.stacks.factory(**stack_attributes)

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
