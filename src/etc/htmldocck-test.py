#!/usr/bin/env pytnon
# -*- coding: utf-8 -*-

"""
Unit tests for htmldocck.py.
"""

from __future__ import absolute_import, print_function, unicode_literals

from io import StringIO
from os import path
import os
import shutil
import tempfile
import textwrap
import unittest

from htmldocck import Command, Has, Matches, HasDir, FailedCheck, CachedFiles, InvalidCheck
import htmldocck

class CommandParsingTests(unittest.TestCase):
    def test_no_commands(self):
        commands = list(htmldocck.parse_commands(StringIO("fn main() {}")))
        self.assertSequenceEqual(commands, [])


    def test_has(self):
        commands = list(htmldocck.parse_commands(StringIO('// @has foo.html')))
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertEqual(command.assertion.path, 'foo.html')


    def test_matches(self):
        commands = list(htmldocck.parse_commands(StringIO("@matches foo.html '.*'")))
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertEqual(command.assertion.path, 'foo.html')
        self.assertEqual(command.assertion.regex, '.*')


    def test_line_continuation(self):
        src = textwrap.dedent(
            r"""
            // @has a/long/path.html '//*[@class="some class"]' \
            //    'This is some text'
            """)
        command = list(htmldocck.parse_commands(StringIO(src)))[0]
        self.assertEqual(command.assertion.path, 'a/long/path.html')
        self.assertEqual(command.assertion.xpath, '//*[@class="some class"]')
        self.assertEqual(command.assertion.string, 'This is some text')


class CachedFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

        os.mkdir(path.join(self.tmpdir, 'test-dir'))
        with open(path.join(self.tmpdir, 'test-file'), 'w') as f:
            f.write('contents')

        self.cache = CachedFiles(self.tmpdir)


    def tearDown(self):
        shutil.rmtree(self.tmpdir)


    def test_get_file(self):
        self.assertEqual(self.cache.get_file('test-file'), 'contents')

        with self.assertRaises(FailedCheck):
            self.cache.get_file('test-dir')

        with self.assertRaises(FailedCheck):
            self.cache.get_file('nonexistent-file')


    def test_assert_dir(self):
        self.cache.assert_dir('test-dir')

        with self.assertRaises(FailedCheck):
            self.cache.assert_dir('test-file')

        with self.assertRaises(FailedCheck):
            self.cache.assert_dir('nonexistent-dir')


    def test_resolve_path(self):
        cache = CachedFiles(self.tmpdir)

        with self.assertRaises(InvalidCheck) as cm:
            cache.resolve_path('-')

        self.assertEqual(cache.resolve_path('test-file'), 'test-file')
        self.assertEqual(cache.resolve_path('-'), 'test-file')


class DirectiveTests(unittest.TestCase):
    def test_has_xpath_no_string(self):
        with self.assertRaises(ValueError):
            Has('index.html', string=None, xpath='//*')


class HasDirDirectiveTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp();
        os.mkdir(path.join(self.tmpdir, 'test-dir'))
        open(path.join(self.tmpdir, 'test-file'), 'a').close()
        self.cache = CachedFiles(self.tmpdir)


    def tearDown(self):
        shutil.rmtree(self.tmpdir)


    def test_has_dir_exists(self):
        HasDir('test-dir').check(self.cache)


    def test_has_dir_not_exists(self):
        with self.assertRaises(FailedCheck) as cm:
            HasDir('nonexistent-dir').check(self.cache)


    def test_has_dir_not_a_directory(self):
        with self.assertRaises(FailedCheck) as cm:
            HasDir('test-file').check(self.cache)


if __name__ == '__main__':
    unittest.main()
