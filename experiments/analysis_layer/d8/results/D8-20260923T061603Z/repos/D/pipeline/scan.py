def scan(rows):
    for row in rows:
        if corrupt(row):
            break
        check(row)
    commit()


def corrupt(row):
    return row is None


def check(row):
    return row


def commit():
    return True
