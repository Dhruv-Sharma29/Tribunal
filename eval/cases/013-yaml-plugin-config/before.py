import yaml


def load_plugin_config(text):
    """Parse a plugin's config block into a dict."""
    config = yaml.load(text, Loader=yaml.Loader)
    if not isinstance(config, dict):
        raise ValueError("a plugin config must be a mapping")
    return config
