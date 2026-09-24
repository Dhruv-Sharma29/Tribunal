from before import rank_all

ORDERING = ["c", "a", "b"]


def test_ranks_by_position_in_ordering():
    assert rank_all(["a", "b", "c"], ORDERING) == [("c", 0), ("a", 1), ("b", 2)]


def test_output_is_sorted_by_rank():
    ranks = [rank for _, rank in rank_all(["b", "a"], ORDERING)]
    assert ranks == sorted(ranks)
