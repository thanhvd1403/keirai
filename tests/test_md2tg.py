import unittest

import md2tg


def count(html, tag):
    if tag.startswith("</"):
        return html.count(tag)
    return html.count("<%s>" % tag) + html.count("<%s " % tag)


class TestConvert(unittest.TestCase):
    def test_escaping(self):
        out = md2tg.convert("<script>alert('x') & \"quotes\"</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("&amp;", out)

    def test_bold_italic_strike_spoiler(self):
        out = md2tg.convert("**bold** and *em* and ~~gone~~ and ||secret||")
        self.assertIn("<b>bold</b>", out)
        self.assertIn("<i>em</i>", out)
        self.assertIn("<s>gone</s>", out)
        self.assertIn("<tg-spoiler>secret</tg-spoiler>", out)

    def test_snake_case_not_italic(self):
        out = md2tg.convert("snake_case and file_name.txt stay")
        self.assertNotIn("<i>", out)

    def test_inline_code_protected(self):
        out = md2tg.convert("use `x = a**b` here")
        self.assertIn("<code>x = a**b</code>", out)
        self.assertNotIn("<b>", out)

    def test_code_fence_with_lang(self):
        out = md2tg.convert("```python\nprint('<hi & bye>')\n```")
        self.assertIn('<pre><code class="language-python">', out)
        self.assertIn("print('&lt;hi &amp; bye&gt;')", out)

    def test_link(self):
        out = md2tg.convert("see [docs](https://opencode.ai) now")
        self.assertIn('<a href="https://opencode.ai">docs</a>', out)

    def test_bare_url_with_query(self):
        out = md2tg.convert("go to https://example.com?a=1&b=2 ok")
        self.assertIn('<a href="https://example.com?a=1&amp;b=2">https://example.com?a=1&amp;b=2</a>', out)

    def test_header(self):
        self.assertEqual(md2tg.convert("# Title"), "<b>Title</b>")

    def test_blockquote(self):
        out = md2tg.convert("> line one\n> line two")
        self.assertIn("<blockquote>line one\nline two</blockquote>", out)

    def test_bold_in_link_text(self):
        out = md2tg.convert("[**big** deal](https://x.com)")
        self.assertIn('<a href="https://x.com"><b>big</b> deal</a>', out)


class TestSplit(unittest.TestCase):
    def test_short_untouched(self):
        parts = md2tg.split("hello **world**")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0], "hello <b>world</b>")

    def test_many_paragraphs_fit(self):
        md = "\n\n".join("para %d %s" % (i, "x" * 200) for i in range(60))
        parts = md2tg.split(md)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 4096)

    def test_huge_code_block_reopens_fence(self):
        body = "\n".join("line %d of code" % i for i in range(2000))
        md = "before\n```python\n%s\n```\nafter" % body
        parts = md2tg.split(md)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 4096)
            # every chunk containing code must be balanced (no cut-off tags)
            self.assertEqual(count(p, "pre"), count(p, "</pre>"))
        # first and last chunk contain the surrounding text
        self.assertIn("before", parts[0])
        self.assertIn("after", parts[-1])

    def test_monster_line(self):
        md = "word " * 3000  # one huge line
        parts = md2tg.split(md)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 4096)

    def test_empty(self):
        self.assertEqual(md2tg.split(""), [""])


class TestThinking(unittest.TestCase):
    def test_send_parts_thinking_first(self):
        parts = md2tg.send_parts("The answer is **42**.", "Let me think about this...")
        self.assertTrue(parts[0].startswith("<blockquote expandable>"))
        self.assertIn("Let me think", parts[0])
        self.assertIn("The answer is <b>42</b>.", parts[-1])

    def test_send_parts_no_thinking(self):
        parts = md2tg.send_parts("answer only", None)
        self.assertEqual(len(parts), 1)
        self.assertNotIn("blockquote", parts[0])

    def test_send_parts_empty_thinking(self):
        parts = md2tg.send_parts("answer", "  ")
        self.assertEqual(len(parts), 1)

    def test_long_thinking_fits(self):
        thinking = "reasoning line %d\n" * 400 % tuple(range(400))
        answer = "ok"
        parts = md2tg.send_parts(answer, thinking)
        for p in parts:
            self.assertLessEqual(len(p), 4096)


if __name__ == "__main__":
    unittest.main()
