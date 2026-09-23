def process(items):
    for item in items:
        if invalid(item):
            return False
        save(item)
    finalize()
    return True


def invalid(item):
    return item is None


def save(item):
    return item


def finalize():
    return True
