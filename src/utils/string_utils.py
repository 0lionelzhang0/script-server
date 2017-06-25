def replace(old_text, new_text, start, end):
    start_text = old_text[:start]

    if end < (len(old_text) - 1):
        end_text = old_text[end + 1:]
    else:
        end_text = ''

    return start_text + new_text + end_text


def is_integer(text):
    try:
        int(text)
        return True
    except ValueError:
        return False


def unwrap_quotes(string):
    if string.startswith('"') and string.endswith('"'):
        return string[1:-1]
    elif string.startswith("'") and string.endswith("'"):
        return string[1:-1]

    return string
