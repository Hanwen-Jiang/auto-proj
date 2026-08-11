from __future__ import annotations

import secrets
from collections.abc import Sequence
from typing import TypeVar

SHORT_PROBE_QUESTION_MIN_LENGTH = 8
SHORT_PROBE_QUESTION_MAX_LENGTH = 48

_QUESTION_PREFIXES = ("", "Brief: ", "Check: ", "Quick: ", "Simple: ", "Try: ")
_QUESTION_TEMPLATES = (
    "What is {left}+{right}?",
    "{left}+{right} equals?",
    "What is {high}-{low}?",
    "{high}-{low} equals?",
    "What is {factor_left}*{factor_right}?",
    "Larger: {left}/{right}?",
    "Smaller: {left}/{right}?",
    "Is {left} odd or even?",
    "Is {left}>{right}?",
    "Is {low} less than {high}?",
    "Next: {sequence_a},{sequence_b},?",
    "Sort: {left},{right},{third}?",
    "Middle: {left},{right},{third}?",
    "Double {left}?",
    "Half of {even_number}?",
    "One more than {left}?",
    "One less than {left}?",
    "Sides of {shape}: {side_option_a}/{side_option_b}?",
    "Does {shape} have {side_guess} sides?",
    "Which is a shape: {shape_option_a}/{shape_option_b}?",
    "Color of {object_name}: {color_option_a}/{color_option_b}?",
    "Opposite of {adjective}: {opposite_option_a}/{opposite_option_b}?",
    "Plural of {noun}: {plural_option_a}/{plural_option_b}?",
    "First letter of {word}: {letter_option_a}/{letter_option_b}?",
    "Letters in {word}: {length_option_a}/{length_option_b}?",
    "Reverse {short_word}: {reverse_option_a}/{reverse_option_b}?",
    "Which starts with {first_letter}: {word_option_a}/{word_option_b}?",
    "Alphabetically first: {word_option_a}/{word_option_b}?",
    "Longer word: {word_option_a}/{word_option_b}?",
    "Does {word} contain {random_letter}?",
    "Count {random_letter} in {word}?",
    "After {day}: {day_option_a}/{day_option_b}?",
    "After {month}: {month_option_a}/{month_option_b}?",
    "Which is a day: {day_kind_a}/{day_kind_b}?",
    "Which is a month: {month_kind_a}/{month_kind_b}?",
    "Vowel or consonant: {random_letter}?",
    "Uppercase of {random_letter}?",
    "Lowercase of {upper_letter}?",
    "Complete: {letter_a},{letter_b},?",
    "Pick the number: {number_option_a}/{number_option_b}?",
    "Pick the noun: {noun_option_a}/{noun_option_b}?",
    "Pick the adjective: {adjective_option_a}/{adjective_option_b}?",
)
SHORT_PROBE_QUESTION_TYPE_COUNT = len(_QUESTION_TEMPLATES)

_SHAPE_SIDES = (
    ("triangle", 3),
    ("square", 4),
    ("pentagon", 5),
    ("hexagon", 6),
    ("heptagon", 7),
    ("octagon", 8),
)
_OBJECT_COLORS = (
    ("coal", "black"),
    ("grass", "green"),
    ("lemon", "yellow"),
    ("snow", "white"),
    ("the sky", "blue"),
    ("tomato", "red"),
)
_ANTONYM_PAIRS = (
    ("early", "late"),
    ("fast", "slow"),
    ("hot", "cold"),
    ("light", "dark"),
    ("open", "closed"),
    ("soft", "hard"),
    ("up", "down"),
    ("young", "old"),
)
_NOUN_PLURALS = (
    ("book", "books"),
    ("car", "cars"),
    ("chair", "chairs"),
    ("cup", "cups"),
    ("desk", "desks"),
    ("door", "doors"),
    ("lamp", "lamps"),
    ("road", "roads"),
    ("star", "stars"),
    ("train", "trains"),
    ("tree", "trees"),
    ("wheel", "wheels"),
)
_WORDS = (
    "apple",
    "beach",
    "brick",
    "chair",
    "clock",
    "cloud",
    "desk",
    "door",
    "drum",
    "eagle",
    "field",
    "flower",
    "fork",
    "garden",
    "glass",
    "grape",
    "house",
    "index",
    "juice",
    "kite",
    "lamp",
    "lemon",
    "moon",
    "night",
    "ocean",
    "paper",
    "pencil",
    "phone",
    "queen",
    "river",
    "road",
    "shoe",
    "spoon",
    "star",
    "stone",
    "table",
    "train",
    "tree",
    "unit",
    "voice",
    "water",
    "wheel",
    "xray",
    "yarn",
    "zebra",
)
_SHORT_WORDS = (
    "bag",
    "box",
    "bus",
    "cat",
    "cup",
    "day",
    "dog",
    "hat",
    "key",
    "map",
    "net",
    "pen",
    "red",
    "run",
    "sky",
    "sun",
)
_DAY_PAIRS = (
    ("Monday", "Tuesday"),
    ("Tuesday", "Wednesday"),
    ("Wednesday", "Thursday"),
    ("Thursday", "Friday"),
    ("Friday", "Saturday"),
    ("Saturday", "Sunday"),
    ("Sunday", "Monday"),
)
_MONTH_PAIRS = (
    ("January", "February"),
    ("February", "March"),
    ("March", "April"),
    ("April", "May"),
    ("May", "June"),
    ("June", "July"),
    ("July", "August"),
    ("August", "September"),
    ("September", "October"),
    ("October", "November"),
    ("November", "December"),
    ("December", "January"),
)
_LETTERS = tuple("abcdefghijklmnopqrstuvwxyz")

_T = TypeVar("_T")


def _different(value: _T, values: Sequence[_T]) -> _T:
    return secrets.choice(tuple(candidate for candidate in values if candidate != value))


def _shuffled_pair(first: _T, second: _T) -> tuple[_T, _T]:
    return (first, second) if secrets.randbelow(2) == 0 else (second, first)


def build_short_probe_question() -> str:
    """Build one compact ordinary question with independently randomized content."""

    left = 10 + secrets.randbelow(80)
    right = _different(left, tuple(range(10, 90)))
    third = _different(left, tuple(value for value in range(10, 90) if value != right))
    low, high = sorted((left, right))
    sequence_a = 2 + secrets.randbelow(78)
    sequence_b = sequence_a + 1 + secrets.randbelow(5)

    word = secrets.choice(_WORDS)
    other_word = secrets.choice(
        tuple(value for value in _WORDS if value != word and value[0] != word[0])
    )
    short_word = secrets.choice(_SHORT_WORDS)
    other_short_word = _different(short_word, _SHORT_WORDS)
    random_letter = secrets.choice(_LETTERS)
    other_letter = _different(word[0], _LETTERS)

    shape, side_count = secrets.choice(_SHAPE_SIDES)
    side_guess = _different(side_count, tuple(range(3, 11)))
    object_name, color = secrets.choice(_OBJECT_COLORS)
    other_color = _different(color, tuple(value for _, value in _OBJECT_COLORS))
    adjective, opposite = secrets.choice(_ANTONYM_PAIRS)
    other_opposite = _different(opposite, tuple(value for _, value in _ANTONYM_PAIRS))
    noun, plural = secrets.choice(_NOUN_PLURALS)
    other_plural = _different(plural, tuple(value for _, value in _NOUN_PLURALS))

    day, next_day = secrets.choice(_DAY_PAIRS)
    other_day = _different(next_day, tuple(value for value, _ in _DAY_PAIRS))
    month, next_month = secrets.choice(_MONTH_PAIRS)
    other_month = _different(next_month, tuple(value for value, _ in _MONTH_PAIRS))

    letter_index = secrets.randbelow(24)
    letter_a = _LETTERS[letter_index]
    letter_b = _LETTERS[letter_index + 1]
    length_guess = _different(len(word), tuple(range(3, 10)))
    side_options = _shuffled_pair(side_count, side_guess)
    shape_options = _shuffled_pair(shape, word)
    color_options = _shuffled_pair(color, other_color)
    opposite_options = _shuffled_pair(opposite, other_opposite)
    plural_options = _shuffled_pair(plural, other_plural)
    letter_options = _shuffled_pair(word[0], other_letter)
    length_options = _shuffled_pair(len(word), length_guess)
    reverse_options = _shuffled_pair(short_word[::-1], other_short_word[::-1])
    word_options = _shuffled_pair(word, other_word)
    day_options = _shuffled_pair(next_day, other_day)
    month_options = _shuffled_pair(next_month, other_month)
    day_kind_options = _shuffled_pair(day, word)
    month_kind_options = _shuffled_pair(month, word)
    number_options = _shuffled_pair(left, word)
    noun_options = _shuffled_pair(noun, adjective)
    adjective_options = _shuffled_pair(adjective, noun)
    question = secrets.choice(_QUESTION_PREFIXES) + secrets.choice(
        _QUESTION_TEMPLATES
    ).format(
        left=left,
        right=right,
        third=third,
        low=low,
        high=high,
        factor_left=2 + secrets.randbelow(8),
        factor_right=2 + secrets.randbelow(8),
        even_number=2 * (2 + secrets.randbelow(48)),
        sequence_a=sequence_a,
        sequence_b=sequence_b,
        shape=shape,
        side_guess=side_guess,
        side_option_a=side_options[0],
        side_option_b=side_options[1],
        shape_option_a=shape_options[0],
        shape_option_b=shape_options[1],
        object_name=object_name,
        color_option_a=color_options[0],
        color_option_b=color_options[1],
        adjective=adjective,
        opposite_option_a=opposite_options[0],
        opposite_option_b=opposite_options[1],
        noun=noun,
        plural_option_a=plural_options[0],
        plural_option_b=plural_options[1],
        word=word,
        first_letter=word[0],
        letter_option_a=letter_options[0],
        letter_option_b=letter_options[1],
        length_option_a=length_options[0],
        length_option_b=length_options[1],
        short_word=short_word,
        reverse_option_a=reverse_options[0],
        reverse_option_b=reverse_options[1],
        word_option_a=word_options[0],
        word_option_b=word_options[1],
        random_letter=random_letter,
        upper_letter=random_letter.upper(),
        day=day,
        day_option_a=day_options[0],
        day_option_b=day_options[1],
        day_kind_a=day_kind_options[0],
        day_kind_b=day_kind_options[1],
        month=month,
        month_option_a=month_options[0],
        month_option_b=month_options[1],
        month_kind_a=month_kind_options[0],
        month_kind_b=month_kind_options[1],
        number_option_a=number_options[0],
        number_option_b=number_options[1],
        noun_option_a=noun_options[0],
        noun_option_b=noun_options[1],
        adjective_option_a=adjective_options[0],
        adjective_option_b=adjective_options[1],
        letter_a=letter_a,
        letter_b=letter_b,
    )
    if not SHORT_PROBE_QUESTION_MIN_LENGTH <= len(question) <= (
        SHORT_PROBE_QUESTION_MAX_LENGTH
    ):
        raise RuntimeError("short_probe_question_length_invalid")
    return question
