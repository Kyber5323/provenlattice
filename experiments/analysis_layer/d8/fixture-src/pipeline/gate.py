def gate(mode):
    if mode == "fast":
        skip_tests(mode)
    else:
        run_tests(mode)
    publish(mode)


def skip_tests(mode):
    return mode


def run_tests(mode):
    return mode


def publish(mode):
    return mode
