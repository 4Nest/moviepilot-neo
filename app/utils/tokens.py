import re
from typing import Optional


class Tokens:
    _text: str = ""
    _index: int = 0
    _tokens: list = []

    def __init__(self, text):
        self._text = text
        self._tokens = []
        self.load_text(text)

    def load_text(self, text):
        splitted_text = re.split(r"\.|\s+|\(|\)|\[|]|-|【|】|/|～|;|&|\||#|_|「|」|~", text)
        for sub_text in splitted_text:
            if sub_text:
                self._tokens.append(sub_text)

    def cur(self):
        if self._index >= len(self._tokens):
            return None
        return self._tokens[self._index]

    def get_next(self):
        token = self.cur()
        if token:
            self._index = self._index + 1
        return token

    def prev(self) -> Optional[str]:
        """
        返回正在处理词元的前一个词元但不移动游标。

        主循环以 get_next() 先消费当前词元再交给各识别阶段，因此游标已指向
        下一个词元；`cur()` 是后继，本方法的偏移量与之相差一位。
        """
        index = self._index - 2
        if index < 0:
            return None
        return self._tokens[index]

    def peek(self):
        index = self._index + 1
        if index >= len(self._tokens):
            return None
        return self._tokens[index]

    @property
    def tokens(self):
        return self._tokens
