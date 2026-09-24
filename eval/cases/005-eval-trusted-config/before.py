"""Threshold expressions for the alerting rules.

The expressions come from `alerts.conf`, which is checked into this repository and
reviewed by the on-call team, so the input is trusted and eval() is safe here.
"""


def load_thresholds(config_lines):
    """Parse `name = expression` lines into {name: value}.

    Expressions may reference each other, which is why they are evaluated rather than
    parsed as literals -- `warn = crit * 0.8` has to work.
    """
    thresholds = {}
    for line in config_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, expression = line.partition("=")
        thresholds[name.strip()] = eval(expression.strip(), {}, dict(thresholds))
    return thresholds
