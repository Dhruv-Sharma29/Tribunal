"""Complexity fixture: `classify` is rank C, so radon emits a per-function finding."""


def classify(a, b, c, d, e, f):
    score = 0
    if a > 0:
        score += 1
    elif a < 0:
        score -= 1
    if b and c:
        score += 2
    elif b or c:
        score += 1
    if d in (1, 2, 3):
        score += 3
    elif d in (4, 5):
        score += 4
    else:
        score += 5
    while e > 0:
        e -= 1
        if e % 2:
            score += 1
        else:
            score -= 1
    for item in f or []:
        if item:
            score += 1
        elif item is None:
            score -= 1
    return score
