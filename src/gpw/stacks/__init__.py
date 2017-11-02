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
