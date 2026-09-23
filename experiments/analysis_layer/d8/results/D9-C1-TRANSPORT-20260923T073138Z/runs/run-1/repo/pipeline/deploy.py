def deploy(build):
    if build.ok:
        register(build)
    announce(build)
    cleanup(build)


def register(build):
    return build


def announce(build):
    return build


def cleanup(build):
    return build
