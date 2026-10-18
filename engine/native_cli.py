"""Keep native app-server configuration in one CLI argument scope."""

VALUE_OPTIONS = {'-c', '--config', '--enable', '--disable', '-p', '--profile'}


def app_server_index(args):
    """Locate the supported subcommand without mistaking an option's value for it."""
    i = 0
    while i < len(args):
        value = args[i]
        if value in VALUE_OPTIONS:
            i += 2
        elif value.startswith('-c') and len(value) > 2 or any(
                value.startswith(option + '=') for option in VALUE_OPTIONS if option.startswith('--')):
            i += 1
        else:
            break
    return i if i < len(args) and args[i] == 'app-server' else None


def app_server_config_scope(args):
    """Move root -c options after app-server, preserving their precedence.

    Codex 0.163 drops root overrides when the desktop adds a subcommand -c
    (openai/codex#39012). Other root options, e.g. --profile, cannot be moved.
    Only callers already validated as stdio app-server may use this helper.
    """
    index = app_server_index(args)
    if index is None:
        raise ValueError('Unsupported native app-server command prefix')
    prefix, overrides = [], []
    i = 0
    while i < index:
        value = args[i]
        size = 2 if value in VALUE_OPTIONS else 1
        config = value in {'-c', '--config'} or value.startswith('--config=') or (
            value.startswith('-c') and len(value) > 2)
        (overrides if config else prefix).extend(args[i:i + size])
        i += size
    return [*prefix, 'app-server', *overrides, *args[index + 1:]]
