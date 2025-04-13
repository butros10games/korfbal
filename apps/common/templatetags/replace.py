"""Custom template tags for replacing strings in templates."""

from django import template


register = template.Library()


@register.filter
def replace(value, arg):
    """Replace all occurrences of a string in a value with another string.

    Args:
        value: The value to replace strings in.
        arg: The string to replace and the string to replace it with separated by a pipe.

    Returns:
        The value with the strings replaced.

    """
    if len(arg.split("|")) != 2:
        return value

    what, to = arg.split("|")
    return value.replace(what, to)
