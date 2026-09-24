from before import largest_per_owner

AVATARS = {
    "ana/one": b"xx",
    "ana/two": b"xxxx",
    "bo/one": b"x",
}


def test_picks_the_largest_per_owner():
    assert largest_per_owner(AVATARS, ["ana", "bo"]) == {
        "ana": "ana/two",
        "bo": "bo/one",
    }


def test_an_owner_with_no_avatars_maps_to_none():
    assert largest_per_owner(AVATARS, ["cy"]) == {"cy": None}
