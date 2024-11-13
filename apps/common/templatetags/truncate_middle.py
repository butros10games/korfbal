from django import template

register = template.Library()

@register.filter
def truncate_middle(text, max_length):
    """
    Truncate middle filter
    Use `{{ "Some long text"|truncate_middle:10 }}`
    """
    
    text = str(text)
    
    if len(text) <= max_length:
        return text

    # Calculate the number of characters to show before and after the ellipsis
    chars_to_show = max_length - 3
    front_chars = chars_to_show // 2
    back_chars = chars_to_show - front_chars

    return text[:front_chars] + "..." + text[-back_chars:]
