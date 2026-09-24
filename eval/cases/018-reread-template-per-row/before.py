def render_rows(rows, template_path):
    """Render each row against the template file and return the rendered strings."""
    rendered = []
    for row in rows:
        with open(template_path, encoding="utf-8") as handle:
            template = handle.read()
        rendered.append(template.format(**row))
    return rendered
