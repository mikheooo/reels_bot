from app.worker.text_stats import word_count


def test_word_count_empty_string():
    assert word_count("") == 0


def test_word_count_whitespace_only():
    assert word_count("   \t\n  ") == 0


def test_word_count_single_word():
    assert word_count("hello") == 1


def test_word_count_multiple_words():
    assert word_count("hello world foo") == 3


def test_word_count_multiple_spaces_and_tabs():
    assert word_count("  hello\t\t  world \n  foo  ") == 3
