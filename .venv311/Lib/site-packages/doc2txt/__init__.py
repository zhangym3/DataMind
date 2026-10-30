"""doc2txt - Extract text from Microsoft Word documents using antiword."""

from .antiword_wrapper import extract_text, get_antiword_binary
from .text_optimizer import optimize_text

__all__ = ['extract_text', 'get_antiword_binary', 'optimize_text']