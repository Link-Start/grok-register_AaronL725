#!/usr/bin/env python3
import ast
from pathlib import Path

path = Path(__file__).resolve().with_name("apply_post_modularization_fixes.py")
text = path.read_text(encoding="utf-8")

helper_anchor = '''def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)
'''

# Raw text is intentional: the generated target must contain the two-character
# escape sequence "\\n" inside its Python string literal, not an actual newline.
helper_definition = r'''

def replace_in_function(text, function_name, old, new, label):
    marker = f"def {function_name}("
    start = text.find(marker)
    if start < 0:
        raise RuntimeError(f"{label}: function {function_name!r} not found")
    next_function = text.find("\ndef ", start + len(marker))
    end = len(text) if next_function < 0 else next_function + 1
    function_text = text[start:end]
    count = function_text.count(old)
    if count != 1:
        raise RuntimeError(
            f"{label}: expected one match in {function_name}, got {count}"
        )
    return text[:start] + function_text.replace(old, new, 1) + text[end:]
'''

if "def replace_in_function(" not in text:
    if text.count(helper_anchor) != 1:
        raise RuntimeError("replace helper anchor is not unique")
    text = text.replace(helper_anchor, helper_anchor + helper_definition, 1)

replacements = (
    (
        '''text = replace_once(text, old_duck_body, '            combined = normalize_mail_body(detail)\\n', "DuckMail body normalization")''',
        '''text = replace_in_function(
    text,
    "duckmail_get_oai_code",
    old_duck_body,
    '            combined = normalize_mail_body(detail)\\n',
    "DuckMail body normalization",
)''',
        "DuckMail",
    ),
    (
        '''text = replace_once(text, old_yyds_body, '            combined = normalize_mail_body(detail)\\n', "YYDS body normalization")''',
        '''text = replace_in_function(
    text,
    "yyds_get_oai_code",
    old_yyds_body,
    '            combined = normalize_mail_body(detail)\\n',
    "YYDS body normalization",
)''',
        "YYDS",
    ),
)

for old, new, label in replacements:
    if old in text:
        if text.count(old) != 1:
            raise RuntimeError(f"{label} replacement call is not unique")
        text = text.replace(old, new, 1)
    elif new not in text:
        raise RuntimeError(f"{label} replacement call anchor not found")

for forbidden in (
    "replace_once(text, old_duck_body",
    "replace_once(text, old_yyds_body",
):
    if forbidden in text:
        raise RuntimeError(f"ambiguous provider replacement remains: {forbidden}")

# Catch all quoting, escaping, and generated-source syntax errors before writing.
ast.parse(text, filename=str(path))
path.write_text(text, encoding="utf-8")
print("post-modularization patcher repaired and syntax-validated")
