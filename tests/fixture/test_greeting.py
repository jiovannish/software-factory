import unittest

from greeting import greet


class GreetingTest(unittest.TestCase):
    def test_greeting(self):
        self.assertEqual(greet("Jio"), "Hello, Jio!")
