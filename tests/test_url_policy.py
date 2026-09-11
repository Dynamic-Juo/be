import pytest

from deepcheck.errors import UnsupportedURLError
from deepcheck.url_policy import normalize_youtube_url


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=cYRkZmBuDqI",
    "http://youtube.com/watch?v=cYRkZmBuDqI&t=10&list=other",
    "https://m.youtube.com/shorts/cYRkZmBuDqI?si=tracking",
    "https://youtu.be/cYRkZmBuDqI#fragment",
])
def test_supported_video_urls_are_canonicalized(url):
    assert normalize_youtube_url(url) == "https://www.youtube.com/watch?v=cYRkZmBuDqI"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "https://example.org/movie.mp4", "http://localhost/",
    "http://2130706433/", "http://[::ffff:127.0.0.1]/", "http://169.254.169.254/",
    "https://www.youtube.com:8000/watch?v=cYRkZmBuDqI",
    "https://user:password@www.youtube.com/watch?v=cYRkZmBuDqI",
    "https://www.youtube.com@internal/watch?v=cYRkZmBuDqI",
    "https://www.youtube.com.evil.invalid/watch?v=cYRkZmBuDqI",
    "https://www.youtube.com/redirect?q=http://localhost",
    "https://www.youtube.com/watch?v=cYRkZmBuDqI&v=cYRkZmBuDqI",
    "https://www.youtube.com/playlist?list=cYRkZmBuDqI",
    "https://youtu.be/cYRkZmBuDqI/extra", "https://youtu.be/not-an-id",
    "https://www.you\ntube.com/watch?v=cYRkZmBuDqI",
    "https://www.youtube.com/watch?v=cYRkZmBuDqI\t", "http://[invalid",
])
def test_arbitrary_targets_and_ambiguous_urls_are_rejected(url):
    with pytest.raises(UnsupportedURLError):
        normalize_youtube_url(url)
