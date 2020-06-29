#!/usr/bin/env python
# -*- coding: utf-8 -*-

r"""
htmldocck.py is a custom checker script for Rustdoc HTML outputs.

# How and why?

The principle is simple: This script receives a path to generated HTML
documentation and a "template" script, which has a series of check
commands like `@has` or `@matches`. Each command is used to check if
some pattern is present or not present in the particular file or in
a particular node of the HTML tree. In many cases, the template script
happens to be the source code given to rustdoc.

While it indeed is possible to test in smaller portions, it has been
hard to construct tests in this fashion and major rendering errors were
discovered much later. This script is designed to make black-box and
regression testing of Rustdoc easy. This does not preclude the needs for
unit testing, but can be used to complement related tests by quickly
showing the expected renderings.

In order to avoid one-off dependencies for this task, this script uses
a reasonably working HTML parser and the existing XPath implementation
from Python's standard library. Hopefully, we won't render
non-well-formed HTML.

# Commands

Commands start with an `@` followed by a command name (letters and
hyphens), and zero or more arguments separated by one or more whitespace
characters and optionally delimited with single or double quotes. The `@`
mark cannot be preceded by a non-whitespace character. Other lines
(including every text up to the first `@`) are ignored, but it is
recommended to avoid the use of `@` in the template file.

There are a number of supported commands:

* `@has PATH` checks for the existence of the given file.

  `PATH` is relative to the output directory. It can be given as `-`
  which repeats the most recently used `PATH`.

* `@has PATH PATTERN` and `@matches PATH PATTERN` checks for
  the occurrence of the given pattern `PATTERN` in the specified file.
  Only one occurrence of the pattern is enough.

  For `@has`, `PATTERN` is a whitespace-normalized (every consecutive
  whitespace being replaced by one single space character) string.
  The entire file is also whitespace-normalized including newlines.

  For `@matches`, `PATTERN` is a Python-supported regular expression.
  The file remains intact but the regexp is matched without the `MULTILINE`
  and `IGNORECASE` options. You can still use a prefix `(?m)` or `(?i)`
  to override them, and `\A` and `\Z` for definitely matching
  the beginning and end of the file.

  (The same distinction goes to other variants of these commands.)

* `@has PATH XPATH PATTERN` and `@matches PATH XPATH PATTERN` checks for
  the presence of the given XPath `XPATH` in the specified HTML file,
  and also the occurrence of the given pattern `PATTERN` in the matching
  node or attribute. Only one occurrence of the pattern in the match
  is enough.

  `PATH` should be a valid and well-formed HTML file. It does *not*
  accept arbitrary HTML5; it should have matching open and close tags
  and correct entity references at least.

  `XPATH` is an XPath expression to match. The XPath is fairly limited:
  `tag`, `*`, `.`, `//`, `..`, `[@attr]`, `[@attr='value']`, `[tag]`,
  `[POS]` (element located in given `POS`), `[last()-POS]`, `text()`
  and `@attr` (both as the last segment) are supported. Some examples:

  - `//pre` or `.//pre` matches any element with a name `pre`.
  - `//a[@href]` matches any element with an `href` attribute.
  - `//*[@class="impl"]//code` matches any element with a name `code`,
    which is an ancestor of some element which `class` attr is `impl`.
  - `//h1[@class="fqn"]/span[1]/a[last()]/@class` matches a value of
    `class` attribute in the last `a` element (can be followed by more
    elements that are not `a`) inside the first `span` in the `h1` with
    a class of `fqn`. Note that there cannot be any additional elements
    between them due to the use of `/` instead of `//`.

  Do not try to use non-absolute paths, it won't work due to the flawed
  ElementTree implementation. The script rejects them.

  For the text matches (i.e. paths not ending with `@attr`), any
  subelements are flattened into one string; this is handy for ignoring
  highlights for example. If you want to simply check for the presence of
  a given node or attribute, use an empty string (`""`) as a `PATTERN`.

* `@count PATH XPATH COUNT' checks for the occurrence of the given XPath
  in the specified file. The number of occurrences must match the given
  count.

* `@has-dir PATH` checks for the existence of the given directory.

All conditions can be negated with `!`. `@!has foo/type.NoSuch.html`
checks if the given file does not exist, for example.

"""

from __future__ import absolute_import, print_function, unicode_literals

import codecs
import io
import sys
import os.path
import re
import shlex
from abc import ABCMeta, abstractmethod
from collections import namedtuple
try:
    from html.parser import HTMLParser
except ImportError:
    from HTMLParser import HTMLParser
from xml.etree import cElementTree as ET

try:
    from html.entities import name2codepoint
except ImportError:
    from htmlentitydefs import name2codepoint

# "void elements" (no closing tag) from the HTML Standard section 12.1.2
VOID_ELEMENTS = set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'keygen',
                     'link', 'menuitem', 'meta', 'param', 'source', 'track', 'wbr'])

# Python 2 -> 3 compatibility
try:
    unichr
except NameError:
    unichr = chr


class CustomHTMLParser(HTMLParser):
    """simplified HTML parser.

    this is possible because we are dealing with very regular HTML from
    rustdoc; we only have to deal with i) void elements and ii) empty
    attributes."""
    def __init__(self, target=None):
        HTMLParser.__init__(self)
        self.__builder = target or ET.TreeBuilder()

    def handle_starttag(self, tag, attrs):
        attrs = dict((k, v or '') for k, v in attrs)
        self.__builder.start(tag, attrs)
        if tag in VOID_ELEMENTS:
            self.__builder.end(tag)

    def handle_endtag(self, tag):
        self.__builder.end(tag)

    def handle_startendtag(self, tag, attrs):
        attrs = dict((k, v or '') for k, v in attrs)
        self.__builder.start(tag, attrs)
        self.__builder.end(tag)

    def handle_data(self, data):
        self.__builder.data(data)

    def handle_entityref(self, name):
        self.__builder.data(unichr(name2codepoint[name]))

    def handle_charref(self, name):
        code = int(name[1:], 16) if name.startswith(('x', 'X')) else int(name, 10)
        self.__builder.data(unichr(code))

    def close(self):
        HTMLParser.close(self)
        return self.__builder.close()


Command = namedtuple('Command', 'negated assertion line_number context')


class FailedCheck(Exception):
    pass


class InvalidCheck(Exception):
    pass


def concat_multi_lines(f):
    """returns a generator out of the file object, which
    - removes `\\` then `\n` then a shared prefix with the previous line then
      optional whitespace;
    - keeps a line number (starting from 0) of the first line being
      concatenated."""
    lastline = None  # set to the last line when the last line has a backslash
    firstlineno = None
    catenated = ''
    for lineno, line in enumerate(f):
        line = line.rstrip('\r\n')

        # strip the common prefix from the current line if needed
        if lastline is not None:
            common_prefix = os.path.commonprefix([line, lastline])
            line = line[len(common_prefix):].lstrip()

        firstlineno = firstlineno or lineno
        if line.endswith('\\'):
            if lastline is None:
                lastline = line[:-1]
            catenated += line[:-1]
        else:
            yield firstlineno, catenated + line
            lastline = None
            firstlineno = None
            catenated = ''

    if lastline is not None:
        print_err(lineno, line, 'Trailing backslash at the end of the file')


LINE_PATTERN = re.compile(r'''
    (?<=(?<!\S)@)(?P<negated>!?)
    (?P<cmd>[A-Za-z]+(?:-[A-Za-z]+)*)
    (?P<args>.*)$
''', re.X | re.UNICODE)


def parse_commands(source_file):
    for line_number, line in concat_multi_lines(source_file):
        m = LINE_PATTERN.search(line)
        if not m:
            continue

        negated = (m.group('negated') == '!')

        cmd = m.group('cmd')
        args = m.group('args')
        if args and not args[:1].isspace():
            print_err(lineno, line, 'Invalid template syntax')
            continue
        try:
            args = shlex.split(args)
        except UnicodeEncodeError:
            args = [arg.decode('utf-8') for arg in shlex.split(args.encode('utf-8'))]

        if cmd == 'has':
            if len(args) == 1:
                assertion = Has(path=args[0])
            elif len(args) == 2:
                assertion = Has(path=args[0], string=args[1])
            elif len(args) == 3:
                assertion = Has(path=args[0], xpath=args[1], string=args[2])
            else:
                raise InvalidCheck(textwrap.dedent("""\
                    Valid @has assertions:
                      - @has PATH
                      - @has PATH STRING
                      - @has PATH XPATH STRING
                    """))
        elif cmd == 'matches':
            if len(args) == 2:
                assertion = Matches(path=args[0], regex=args[1])
            elif len(args) == 3:
                assertion = Matches(path=args[0], xpath=args[1], regex=args[2])
            else:
                raise InvalidCheck(textwrap.dedent("""\
                    Valid @matches assertions:
                      - @matches PATH REGEX
                      - @matches PATH XPATH REGEX
                    """))
        else:
            raise InvalidCheck('Unrecognized command: @{}'.format(cmd))

        yield Command(negated=negated, assertion=assertion,
                      line_number=line_number+1, context=line)


def _flatten(node, acc):
    if node.text:
        acc.append(node.text)
    for e in node:
        _flatten(e, acc)
        if e.tail:
            acc.append(e.tail)


def flatten(node):
    acc = []
    _flatten(node, acc)
    return ''.join(acc)


def normalize_xpath(path):
    if path.startswith('//'):
        return '.' + path  # avoid warnings
    elif path.startswith('.//'):
        return path
    else:
        raise InvalidCheck('Non-absolute XPath is not supported due to implementation issues')


class CachedFiles(object):
    def __init__(self, root):
        self.root = root
        self.files = {}
        self.trees = {}
        self.last_path = None


    def resolve_path(self, path):
        if path != '-':
            path = os.path.normpath(path)
            self.last_path = path
            return path
        elif self.last_path is None:
            raise InvalidCheck('Used "-" in the first command')
        else:
            return self.last_path


    def get_file(self, path):
        """
        Reads the contents of a file, caches it, and returns the contents.

        Raises OSError if the path is a directory or cannot be read.
        """
        path = self.resolve_path(path)
        if path in self.files:
            return self.files[path]

        abspath = os.path.join(self.root, path)

        try:
            with io.open(abspath, encoding='utf-8') as f:
                data = f.read()
                self.files[path] = data
                return data
        except IOError as e:
            raise FailedCheck(e)


    def get_tree(self, path):
        path = self.resolve_path(path)
        if path in self.trees:
            return self.trees[path]

        abspath = os.path.join(self.root, path)
        if not(os.path.exists(abspath) and os.path.isfile(abspath)):
            raise FailedCheck('File does not exist {!r}'.format(path))

        with io.open(abspath, encoding='utf-8') as f:
            try:
                tree = ET.fromstringlist(f.readlines(), CustomHTMLParser())
            except Exception as e:
                raise RuntimeError('Cannot parse an HTML file {!r}: {}'.format(path, e))
            self.trees[path] = tree
            return self.trees[path]


    def assert_dir(self, path):
        "Raises an OSError if the path does not exist or is not a directory."
        path = self.resolve_path(path)
        abspath = os.path.join(self.root, path)

        if not os.path.isdir(abspath):
            raise FailedCheck('{!r} is not a directory'.format(abspath))


def check_string(data, pat, regexp):
    if not pat:
        return True  # special case a presence testing
    elif regexp:
        return re.search(pat, data, flags=re.UNICODE) is not None
    else:
        data = ' '.join(data.split())
        pat = ' '.join(pat.split())
        return pat in data


def check_tree_attr(tree, path, attr, pat, regexp):
    path = normalize_xpath(path)
    ret = False
    for e in tree.findall(path):
        if attr in e.attrib:
            value = e.attrib[attr]
        else:
            continue

        ret = check_string(value, pat, regexp)
        if ret:
            break
    return ret


def check_tree_text(tree, path, pat, regexp):
    path = normalize_xpath(path)
    ret = False
    try:
        for e in tree.findall(path):
            try:
                value = flatten(e)
            except KeyError:
                continue
            else:
                ret = check_string(value, pat, regexp)
                if ret:
                    break
    except Exception:
        print('Failed to get path "{}"'.format(path))
        raise
    return ret


def get_tree_count(tree, path):
    path = normalize_xpath(path)
    return len(tree.findall(path))


def stderr(*args):
    if sys.version_info.major < 3:
        file = codecs.getwriter('utf-8')(sys.stderr)
    else:
        file = sys.stderr

    print(*args, file=file)


def print_err(lineno, context, err, message=None):
    global ERR_COUNT
    ERR_COUNT += 1
    stderr("{}: {}".format(lineno, message or err))
    if message and err:
        stderr("\t{}".format(err))

    if context:
        stderr("\t{}".format(context))


ERR_COUNT = 0


class Directive:
    __metaclass__ = ABCMeta

    @abstractmethod
    def check(self, cache):
        pass


class Has(Directive):
    def __init__(self, path, string=None, xpath=None):
        if xpath is not None and string is None:
            raise ValueError('cannot specify XPath with no string')

        self.path = path
        self.string = string
        self.xpath = xpath


    def check(self, cache):
        if string is None and xpath is None:
            cache.get_file(self.path)
        elif xpath is None:
            if self.string not in cache.get_file(self.path):
                raise FailedCheck('"{}" not found in {}'.format(self.pattern, self.path))
        else:
            raise NotImplemented



class Matches(Directive):
    def __init__(self, path, regex, xpath=None):
        self.path = path
        self.regex= regex
        self.xpath = xpath


    def check(self, cache):
        if xpath is None:
            if re.search(self.regex, cache.get_file(self.path), flags=re.UNICODE) is None:
                raise FailedCheck('no match found')
        else:
            raise NotImplemented


class Count(Directive):
    """
    Checks for the occurrence of the given XPath in the specified file.

    The number of occurrences must match the given count.
    """

    def __init__(path, xpath, count):
        self.path = path
        self.xpath = xpath
        self.count = count

        # FIXME: Disallow negation?


class HasDir(Directive):
    "Checks for the existence of the given directory."

    def __init__(self, path):
        self.path = path

    def check(self, cache):
        cache.assert_dir(self.path)


def check_command(c, cache):
    try:
        cerr = ""
        if c.cmd == 'has' or c.cmd == 'matches':  # string test
            regexp = (c.cmd == 'matches')
            if len(c.args) == 1 and not regexp:  # @has <path> = file existence
                try:
                    cache.get_file(c.args[0])
                    ret = True
                except FailedCheck as err:
                    cerr = str(err)
                    ret = False
            elif len(c.args) == 2:  # @has/matches <path> <pat> = string test
                cerr = "`PATTERN` did not match"
                ret = check_string(cache.get_file(c.args[0]), c.args[1], regexp)
            elif len(c.args) == 3:  # @has/matches <path> <pat> <match> = XML tree test
                cerr = "`XPATH PATTERN` did not match"
                tree = cache.get_tree(c.args[0])
                pat, sep, attr = c.args[1].partition('/@')
                if sep:  # attribute
                    tree = cache.get_tree(c.args[0])
                    ret = check_tree_attr(tree, pat, attr, c.args[2], regexp)
                else:  # normalized text
                    pat = c.args[1]
                    if pat.endswith('/text()'):
                        pat = pat[:-7]
                    ret = check_tree_text(cache.get_tree(c.args[0]), pat, c.args[2], regexp)
            else:
                raise InvalidCheck('Invalid number of @{} arguments'.format(c.cmd))

        elif c.cmd == 'count':  # count test
            if len(c.args) == 3:  # @count <path> <pat> <count> = count test
                expected = int(c.args[2])
                found = get_tree_count(cache.get_tree(c.args[0]), c.args[1])
                cerr = "Expected {} occurrences but found {}".format(expected, found)
                ret = expected == found
            else:
                raise InvalidCheck('Invalid number of @{} arguments'.format(c.cmd))
        elif c.cmd == 'has-dir':  # has-dir test
            if len(c.args) == 1:  # @has-dir <path> = has-dir test
                try:
                    cache.get_dir(c.args[0])
                    ret = True
                except FailedCheck as err:
                    cerr = str(err)
                    ret = False
            else:
                raise InvalidCheck('Invalid number of @{} arguments'.format(c.cmd))
        elif c.cmd == 'valid-html':
            raise InvalidCheck('Unimplemented @valid-html')

        elif c.cmd == 'valid-links':
            raise InvalidCheck('Unimplemented @valid-links')
        else:
            raise InvalidCheck('Unrecognized @{}'.format(c.cmd))

        if ret == c.negated:
            raise FailedCheck(cerr)

    except FailedCheck as err:
        message = '@{}{} check failed'.format('!' if c.negated else '', c.cmd)
        print_err(c.lineno, c.context, str(err), message)
    except InvalidCheck as err:
        print_err(c.lineno, c.context, str(err))


def check(target, commands):
    cache = CachedFiles(target)
    for c in commands:
        check_command(c, cache)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        stderr('Usage: {} <doc dir> <template>'.format(sys.argv[0]))
        raise SystemExit(1)

    target = sys.argv[1]
    template = sys.argv[2]

    cache = CachedFiles(target)
    err_count = 0
    for command in parse_commands(template):
        try:
            command.check(cache)
        except FailedCheck as failed_check:
            if not negated:
                err_count += 1
                stderr("{}: {}", command.line_number, failed_check)
                stderr(command.context)


    if err_count:
        stderr("\nEncountered {} errors".format(ERR_COUNT))
        raise SystemExit(1)
