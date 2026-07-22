# QA PROOF for #72, reverted by the next commit: a new test file added to the
# tree but not listed in TEST_FILES. Adding a file must fail until it is listed.
def test_proof72_new_file_not_in_the_manifest() -> None:
    assert True
