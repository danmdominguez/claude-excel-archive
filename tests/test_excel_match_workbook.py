from excel_archive.match_workbook import infer_workbook_names_from_blob, pick_workbook_name


def test_infer_workbook_name_from_blob_text():
    blob = b"Model repair in progress on EF Shop Model DD.xlsx. Also referenced Other.xlsx once."
    counts = infer_workbook_names_from_blob(blob)
    assert counts.get("EF Shop Model DD.xlsx") == 1
    assert counts.get("Other.xlsx") == 1
    # ambiguous (1 vs 1)
    assert pick_workbook_name(counts) is None


def test_pick_unambiguous_workbook_name():
    blob = b"EF Shop Model DD.xlsx " * 10 + b"Other.xlsx " * 2
    counts = infer_workbook_names_from_blob(blob)
    picked = pick_workbook_name(counts)
    assert picked is not None
    assert picked.workbook_name.lower().endswith(".xlsx")

