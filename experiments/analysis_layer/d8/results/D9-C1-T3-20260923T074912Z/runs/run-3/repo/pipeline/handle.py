def handle(x):
    if validate(x):
        persist(x)
    notify(x)


def validate(x):
    return x


def persist(x):
    return x


def notify(x):
    return x
