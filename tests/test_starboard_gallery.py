"""Multi-image gallery rendering tests for build_starboard_message.

Multiple non-spoiler images on the original message should all appear in the
starboard render — one embed each, stacked after the main embed.  Split out
of ``test_starboard_build.py``; shares the message/attachment fakes.
"""
from tests.starboard_test_utils import _run, _FakeAttachment, _FakeMessage
from tle.cogs.starboard import Starboard


class TestImageGallery:
    """Multiple non-spoiler images on the original message should all appear
    in the starboard render — one embed each, stacked after the main embed.
    """

    def test_single_image_no_extra_embeds_added(self):
        """One image → one embed (existing behaviour, no extras)."""
        att = _FakeAttachment('photo.png', url='https://cdn.example.com/a.png')
        msg = _FakeMessage(attachments=[att])
        content, embeds, files = _run(
            Starboard.build_starboard_message(msg, '\N{WHITE MEDIUM STAR}', 5, 0xffaa10))
        assert len(embeds) == 1
        assert embeds[0].image_url == 'https://cdn.example.com/a.png'

    def test_two_images_produce_two_embeds(self):
        atts = [
            _FakeAttachment('a.png', url='https://cdn.example.com/a.png'),
            _FakeAttachment('b.png', url='https://cdn.example.com/b.png'),
        ]
        msg = _FakeMessage(attachments=atts)
        content, embeds, files = _run(
            Starboard.build_starboard_message(msg, '\N{WHITE MEDIUM STAR}', 5, 0xffaa10))
        assert len(embeds) == 2
        assert embeds[0].image_url == 'https://cdn.example.com/a.png'
        assert embeds[1].image_url == 'https://cdn.example.com/b.png'

    def test_four_images_produce_four_embeds(self):
        atts = [_FakeAttachment(f'img{i}.png', url=f'https://cdn.example.com/{i}.png')
                for i in range(4)]
        msg = _FakeMessage(attachments=atts)
        content, embeds, files = _run(
            Starboard.build_starboard_message(msg, '\N{WHITE MEDIUM STAR}', 5, 0xffaa10))
        assert len(embeds) == 4
        urls = [e.image_url for e in embeds]
        assert urls == [f'https://cdn.example.com/{i}.png' for i in range(4)]

    def test_image_order_preserved(self):
        atts = [
            _FakeAttachment('z.png', url='https://cdn.example.com/z.png'),
            _FakeAttachment('a.png', url='https://cdn.example.com/a.png'),
            _FakeAttachment('m.png', url='https://cdn.example.com/m.png'),
        ]
        msg = _FakeMessage(attachments=atts)
        content, embeds, files = _run(
            Starboard.build_starboard_message(msg, '\N{WHITE MEDIUM STAR}', 5, 0xffaa10))
        assert [e.image_url for e in embeds if e.image_url] == [
            'https://cdn.example.com/z.png',
            'https://cdn.example.com/a.png',
            'https://cdn.example.com/m.png',
        ]

    def test_text_and_author_only_on_main_embed(self):
        """Description and author belong on the main embed only — extras stay minimal."""
        atts = [
            _FakeAttachment('a.png', url='https://cdn.example.com/a.png'),
            _FakeAttachment('b.png', url='https://cdn.example.com/b.png'),
        ]
        msg = _FakeMessage(content='look at these', attachments=atts)
        content, embeds, files = _run(
            Starboard.build_starboard_message(msg, '\N{WHITE MEDIUM STAR}', 5, 0xffaa10))
        assert embeds[0].description == 'look at these'
        assert embeds[0].author_data is not None
        # Extra image embeds should have no author/description duplicating the main.
        assert embeds[1].description is None
        assert embeds[1].author_data is None

    def test_spoiler_images_uploaded_as_files_not_embedded(self):
        atts = [
            _FakeAttachment('a.png', url='https://cdn.example.com/a.png'),
            _FakeAttachment('SPOILER_secret.png', url='https://cdn.example.com/s.png'),
            _FakeAttachment('b.png', url='https://cdn.example.com/b.png'),
        ]
        msg = _FakeMessage(attachments=atts)
        content, embeds, files = _run(
            Starboard.build_starboard_message(msg, '\N{WHITE MEDIUM STAR}', 5, 0xffaa10))
        embedded_urls = [e.image_url for e in embeds if e.image_url]
        assert 'https://cdn.example.com/a.png' in embedded_urls
        assert 'https://cdn.example.com/b.png' in embedded_urls
        assert 'https://cdn.example.com/s.png' not in embedded_urls
        assert any('SPOILER_secret.png' in str(f) for f in files)

    def test_video_and_extra_images_coexist(self):
        """Video → uploaded as file. Non-spoiler images still embed."""
        atts = [
            _FakeAttachment('clip.mp4', url='https://cdn.example.com/clip.mp4'),
            _FakeAttachment('a.png', url='https://cdn.example.com/a.png'),
            _FakeAttachment('b.png', url='https://cdn.example.com/b.png'),
        ]
        msg = _FakeMessage(attachments=atts)
        content, embeds, files = _run(
            Starboard.build_starboard_message(msg, '\N{WHITE MEDIUM STAR}', 5, 0xffaa10))
        image_urls = [e.image_url for e in embeds if e.image_url]
        assert 'https://cdn.example.com/a.png' in image_urls
        assert 'https://cdn.example.com/b.png' in image_urls
        assert any('clip.mp4' in str(f) for f in files)

    def test_many_images_respect_10_embed_cap(self):
        """Even with many images, the total embeds stay under Discord's 10-embed cap."""
        atts = [_FakeAttachment(f'img{i}.png', url=f'https://cdn.example.com/{i}.png')
                for i in range(15)]
        msg = _FakeMessage(attachments=atts)
        content, embeds, files = _run(
            Starboard.build_starboard_message(msg, '\N{WHITE MEDIUM STAR}', 5, 0xffaa10))
        assert len(embeds) <= 10

    def test_multiple_url_paste_image_embeds_all_carried_over(self):
        """When user pastes multiple image URLs, Discord auto-creates image
        embeds. Carry all of them into the starboard render."""
        class FakeImageEmbed:
            def __init__(self, url):
                self.type = 'image'
                self.url = url
                self.image = None
                self.thumbnail = None

        msg = _FakeMessage(content='check these',
                           embeds=[FakeImageEmbed('https://example.com/1.png'),
                                   FakeImageEmbed('https://example.com/2.png'),
                                   FakeImageEmbed('https://example.com/3.png')])
        content, embeds, files = _run(
            Starboard.build_starboard_message(msg, '\N{WHITE MEDIUM STAR}', 5, 0xffaa10))
        embedded_urls = [e.image_url for e in embeds if e.image_url]
        assert 'https://example.com/1.png' in embedded_urls
        assert 'https://example.com/2.png' in embedded_urls
        assert 'https://example.com/3.png' in embedded_urls

    def test_single_url_paste_image_still_works(self):
        """A single URL-paste image keeps the old single-embed behaviour."""
        class FakeImageEmbed:
            type = 'image'
            url = 'https://example.com/only.png'
            image = None
            thumbnail = None

        msg = _FakeMessage(content='one', embeds=[FakeImageEmbed()])
        content, embeds, files = _run(
            Starboard.build_starboard_message(msg, '\N{WHITE MEDIUM STAR}', 5, 0xffaa10))
        assert len(embeds) == 1
        assert embeds[0].image_url == 'https://example.com/only.png'
