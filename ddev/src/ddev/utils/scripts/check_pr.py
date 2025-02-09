"""
Running this script by itself must not use any external dependencies.
"""
# (C) Datadog, Inc. 2023-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import os
import re
import sys
from typing import Iterator


def changelog_entry_suffix(pr_number: int, pr_url: str) -> str:
    return f' ([#{pr_number}]({pr_url}))'


def requires_changelog(target: str, files: Iterator[str]) -> bool:
    if target == 'ddev':
        source = 'src/ddev/'
    else:
        if target == 'datadog_checks_base':
            directory = 'base'
        elif target == 'datadog_checks_dev':
            directory = 'dev'
        elif target == 'datadog_checks_downloader':
            directory = 'downloader'
        else:
            directory = target.replace('-', '_')

        source = f'datadog_checks/{directory}/'

    return any(f.startswith(source) or f == 'pyproject.toml' for f in files)


def git(*args) -> str:
    import subprocess

    try:
        process = subprocess.run(
            ['git', *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', check=True
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f'{str(e)[:-1]}:\n{e.output}') from None

    return process.stdout


def get_added_lines(git_diff: str) -> dict[str, dict[int, str]]:
    files: dict[str, dict[int, str]] = {}
    for modification in re.split(r'^diff --git ', git_diff, flags=re.MULTILINE):
        if not modification:
            continue

        # a/file b/file
        # new file mode 100644
        # index 0000000000..089fd64579
        # --- a/file
        # +++ b/file
        metadata, *blocks = re.split(r'^@@ ', modification, flags=re.MULTILINE)
        *_, before, after = metadata.strip().splitlines()

        # Binary files /dev/null and b/foo/archive.tar.gz differ
        binary_indicator = 'Binary files '
        if after.startswith(binary_indicator):
            line = after[len(binary_indicator) :].rsplit(maxsplit=1)[0]
            if line.startswith('/dev/null and '):
                filename = line.split(maxsplit=2)[-1][2:]
            elif line.endswith(' and /dev/null'):
                filename = line.split(maxsplit=2)[0][2:]
            else:
                _, _, filename = line.partition(' and b/')

            files[filename] = {}
            continue

        # --- a/file
        # +++ /dev/null
        before = before.split(maxsplit=1)[1]
        after = after.split(maxsplit=1)[1]
        filename = before[2:] if after == '/dev/null' else after[2:]
        added = files[filename] = {}

        for block in blocks:
            # -13,3 +13,8 @@
            info, *lines = block.splitlines()
            # third number
            start = int(info.split()[1].split(',')[0][1:])

            removed = 0
            for i, line in enumerate(lines, start):
                if line.startswith('+'):
                    added[i - removed] = line[1:]
                elif line.startswith('-'):
                    removed += 1

    return files


def get_changelog_errors(git_diff: str, suffix: str, private: bool = False) -> list[tuple[str, int, str]]:
    targets: dict[str, dict[str, dict[int, str]]] = {}
    for filename, lines in get_added_lines(git_diff).items():
        target, _, path = filename.partition('/')
        if not path:
            continue

        targets.setdefault(target, {})[path] = lines

    errors: list[tuple[str, int, str]] = []
    for target, files in sorted(targets.items()):
        if not requires_changelog(target, iter(files)):
            continue

        changelog_file = 'CHANGELOG.md'
        if changelog_file not in files:
            errors.append((f'{target}/{changelog_file}', 1, 'Missing changelog entry'))
            continue

        added_lines = files[changelog_file]
        line_numbers_missing_suffix = []
        lines_with_suffix = 0
        for line_number, line in added_lines.items():
            if not line.startswith('* '):
                continue
            elif line.endswith(suffix):
                lines_with_suffix += 1
            else:
                line_numbers_missing_suffix.append(line_number)

        if lines_with_suffix == len(line_numbers_missing_suffix) == 0:
            errors.append((f'{target}/{changelog_file}', 1, 'Missing changelog entry'))
        elif not private and line_numbers_missing_suffix:
            for line_number in line_numbers_missing_suffix:
                errors.append(
                    (
                        f'{target}/{changelog_file}',
                        line_number,
                        f'The first line of every new changelog entry must '
                        f'end with a link to the associated PR:\n`{suffix}`',
                    )
                )

    return errors


def changelog_impl(*, ref: str, diff_file: str, pr_file: str, private: bool) -> None:
    import json

    on_ci = os.environ.get('GITHUB_ACTIONS') == 'true'
    if on_ci:
        with open(diff_file, encoding='utf-8') as f:
            git_diff = f.read()

        with open(pr_file, 'r', encoding='utf-8') as f:
            pr = json.loads(f.read())['pull_request']

        pr_number = pr['number']
        pr_url = pr['html_url']
        pr_labels = [label['name'] for label in pr['labels']]
    else:
        git_diff = git('diff', f'{ref}...')
        pr_number = 1
        pr_url = f'https://github.com/DataDog/integrations-core/pull/{pr_number}'
        pr_labels = []

    if 'changelog/no-changelog' in pr_labels:
        print('No changelog entries required (changelog/no-changelog label found)')
        return

    errors = get_changelog_errors(git_diff, changelog_entry_suffix(pr_number, pr_url), private=private)
    if not errors:
        return
    elif os.environ.get('GITHUB_ACTIONS') == 'true':
        for relative_path, line_number, message in errors:
            message = '%0A'.join(message.splitlines())
            print(f'::error file={relative_path},line={line_number}::{message}')
    else:
        for relative_path, line_number, message in errors:
            print(f'{relative_path}, line {line_number}: {message}')

    sys.exit(1)


def changelog_command(subparsers) -> None:
    parser = subparsers.add_parser('changelog')
    parser.add_argument('--ref', default='origin/master')
    parser.add_argument('--diff-file')
    parser.add_argument('--pr-file')
    parser.add_argument('--private', action='store_true')
    parser.set_defaults(func=changelog_impl)


def main():
    import argparse

    parser = argparse.ArgumentParser(prog=__name__, allow_abbrev=False)
    subparsers = parser.add_subparsers()

    changelog_command(subparsers)

    kwargs = vars(parser.parse_args())
    try:
        # We associate a command function with every subcommand parser.
        # This allows us to emulate Click's command group behavior.
        command = kwargs.pop('func')
    except KeyError:
        parser.print_help()
    else:
        command(**kwargs)


if __name__ == '__main__':
    main()
