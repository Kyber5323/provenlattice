def promote(req):
    if req.failed:
        rollback(req)
    else:
        cleanup(req)


def rollback(req):
    return req


def cleanup(req):
    return req
