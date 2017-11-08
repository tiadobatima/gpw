# Copyright 2017 Gustavo Baratto. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


class BaseStack(object):
    """ Base class for different types of stacks.
    """
    def __init__(self, **kwargs):
        [setattr(self, k, v) for k, v in kwargs.items()]


def factory(**kwargs):
    """ Factory for different types of stacks

    Imports are being don't here so SDKs for multiple providers don't need to
    be installed if never used.
    """

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
        import gpw.stacks.aws
        return gpw.stacks.aws.CloudformationStack(**kwargs)
    elif stack_type == "shell":
        import gpw.stacks.shell
        return gpw.stacks.shell.ShellStack(**kwargs)
    elif stack_type == "gcp":
        import gpw.stacks.gcp
        return gpw.stacks.gcp.GCPStack(**kwargs)
    raise SystemExit("Stack type not supported: {}".format(stack_type))
