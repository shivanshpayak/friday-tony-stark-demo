"""Tests for the fast-path command table."""
import friday.fastpath.registry as registry


def test_no_phrase_collides_across_commands():
    seen = {}
    for cmd in registry.COMMANDS:
        for phrase in cmd.phrases:
            n = registry.normalize(phrase)
            assert n not in seen, f"{n!r} claimed by both {seen[n]} and {cmd.name}"
            seen[n] = cmd.name


def test_every_command_action_is_callable():
    for cmd in registry.COMMANDS:
        assert callable(cmd.action), f"{cmd.name} action is not callable"


def test_command_names_are_unique():
    names = [c.name for c in registry.COMMANDS]
    assert len(names) == len(set(names))


def test_grammar_covers_every_phrase_and_includes_unk():
    grammar = registry.grammar_phrases()
    for cmd in registry.COMMANDS:
        for phrase in cmd.phrases:
            assert registry.normalize(phrase) in grammar
    assert registry.UNK in grammar


def test_grammar_has_no_duplicates():
    grammar = registry.grammar_phrases()
    assert len(grammar) == len(set(grammar))


def test_lookup_finds_command():
    assert registry.lookup("mute").name == "mute"
    assert registry.lookup("next track").name == "next_track"
    assert registry.lookup("sound on").name == "unmute"


def test_lookup_normalizes_case_whitespace_and_punctuation():
    assert registry.lookup("  MUTE.  ").name == "mute"
    assert registry.lookup("NEXT   TRACK").name == "next_track"


def test_lookup_rejects_unk_empty_and_unknown():
    assert registry.lookup(registry.UNK) is None
    assert registry.lookup("") is None
    assert registry.lookup("   ") is None
    assert registry.lookup(None) is None
    assert registry.lookup("what's the weather") is None


def test_grammar_words_are_flattened():
    words = registry.grammar_words()
    assert "next" in words
    assert "track" in words
    assert "next track" not in words


def test_unmute_avoids_the_out_of_vocabulary_word():
    """'unmute' is not in vosk-model-small-en-us-0.15's vocabulary.

    Vosk silently drops unknown words from the grammar, which would make the
    unmute command permanently unmatchable. Guard the regression.
    """
    assert "unmute" not in registry.grammar_words()


def test_command_for_word_identifies_owner():
    assert registry.command_for_word("track") == "next_track"
    assert registry.command_for_word("louder") == "louder"
    assert registry.command_for_word("nonexistent") is None
