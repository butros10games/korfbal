"""Truncate middle template filter."""

from django import template


register = template.Library()


@register.filter
def truncate_middle(text, max_length):
    """Truncate the text in the middle if it is longer than the max_length.

    Args:
        text: The text to truncate.
        max_length: The maximum length of the text.

    Returns:
        The truncated text.

    """
    text = str(text)

    if len(text) <= max_length:
        return text

    # Calculate the number of characters to show before and after the ellipsis
    chars_to_show = max_length - 3
    front_chars = chars_to_show // 2
    back_chars = chars_to_show - front_chars

    return text[:front_chars] + "..." + text[-back_chars:]
