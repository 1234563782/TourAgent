"""Small text presentation primitives shared by terminal and browser answers."""


def single_line(value):
    return " ".join(str(value).splitlines()).strip()


def detail_block(title, content):
    # Prevent supplier text from closing the presentation block early.
    lines = [line if not line.startswith(":::") else " " + line
             for line in content.splitlines()]
    return "\n".join([f":::details {single_line(title)}", *lines, ":::"])


def table_row(values):
    return "| " + " | ".join(single_line(value).replace("|", "｜") for value in values) + " |"
