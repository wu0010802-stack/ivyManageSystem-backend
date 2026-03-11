"""
公告 HTML 清理的單元測試。
確保 _strip_html 在各種 XSS payload 下都能正確移除 HTML 標籤。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api.announcements import _strip_html


class TestStripHtml:
    def test_plain_text_unchanged(self):
        assert _strip_html("一般公告文字") == "一般公告文字"

    def test_script_tag_removed(self):
        """<script> 標籤被移除；標籤內的文字內容（text node）仍保留，但無 tag 即無法執行。"""
        result = _strip_html("<script>alert(1)</script>惡意內容")
        assert "<script>" not in result
        assert "</script>" not in result
        assert "惡意內容" in result

    def test_img_onerror_removed(self):
        """整個含 onerror 的 img 標籤被移除。"""
        result = _strip_html('<img src=x onerror="fetch(\'https://evil.com\')">')
        assert "<img" not in result
        assert "onerror" not in result

    def test_nested_tags_stripped(self):
        result = _strip_html("<b><i>粗斜體</i></b>文字")
        assert "<b>" not in result
        assert "<i>" not in result
        assert "粗斜體文字" in result

    def test_entity_encoded_tag_preserved_as_entity(self):
        """
        &lt;script&gt; 這類 entity-encoded 的輸入，convert_charrefs=False 下
        不會被 decode 成 <script>，而是以 &lt;script&gt; 原樣保留，
        如此即使未來以 v-html 渲染也不會執行。
        """
        result = _strip_html("&lt;script&gt;alert(1)&lt;/script&gt;")
        # 不應被 decode 成真實標籤
        assert "<script>" not in result
        # entity 原樣保留，不執行
        assert "&lt;script&gt;" in result

    def test_empty_string_returns_empty(self):
        assert _strip_html("") == ""

    def test_unclosed_tag(self):
        """未閉合標籤被移除；標籤後的文字保留。"""
        result = _strip_html("<script>alert(1)")
        assert "<script>" not in result

    def test_mixed_content_keeps_text(self):
        content = "本週公告：<b>重要</b>事項，請詳閱。"
        result = _strip_html(content)
        assert result == "本週公告：重要事項，請詳閱。"

    def test_svg_xss_vector_removed(self):
        result = _strip_html('<svg onload=alert(1)></svg>公告')
        assert "onload" not in result
        assert "<svg" not in result
        assert "公告" in result

    def test_entity_img_onerror_not_decoded(self):
        """
        entity-encoded 的 img onerror payload：
        &lt;img src=x onerror=alert(1)&gt;
        不應被 decode 成可執行的 <img>。
        """
        payload = "&lt;img src=x onerror=alert(1)&gt;"
        result = _strip_html(payload)
        # 不含真實 img 標籤
        assert "<img" not in result
        # entity 形式保留（無法被瀏覽器當成 HTML 標籤執行）
        assert "&lt;img" in result
