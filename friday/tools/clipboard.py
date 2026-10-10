"""
Clipboard tools — read from and write to the system clipboard.

Windows goes through the Win32 API via ctypes; Linux through wl-clipboard or
Klipper (see friday/plat.py).
"""

import ctypes

from friday import plat

if plat.IS_WINDOWS:
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

# Windows clipboard format for Unicode text
CF_UNICODETEXT = 13


def _open_clipboard(retries: int = 5) -> bool:
    """Attempt to open the clipboard, retrying if it is locked by another app."""
    import time
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


def register(mcp):

    @mcp.tool()
    def read_clipboard() -> str:
        """
        Read the current text contents of the clipboard.
        Use this when the user asks: "What's in my clipboard?",
        "Read my clipboard", or "What did I copy?".
        """
        if not plat.IS_WINDOWS:
            text = plat.clipboard_get()
            if text is None:
                return "I couldn't access the clipboard on this system, sir."
            if not text.strip():
                return "The clipboard is empty, sir."
            return text

        if not _open_clipboard():
            return "I couldn't access the clipboard — another application may be holding it. Please try again in a moment, sir."

        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                # Check if the clipboard has any data at all
                if user32.CountClipboardFormats() == 0:
                    return "The clipboard is empty, sir."
                return "The clipboard contains data, but it's not text — it may be an image or file. I can only read text content, sir."

            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return "The clipboard appears empty, sir."

            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return "I was unable to read the clipboard contents, sir."

            try:
                text = ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)

            if not text or not text.strip():
                return "The clipboard is empty, sir."

            return text

        finally:
            user32.CloseClipboard()

    @mcp.tool()
    def write_clipboard(text: str) -> str:
        """
        Copy the given text to the clipboard.
        Use this when the user asks: "Copy this to clipboard",
        "Put this in my clipboard", or "Copy X to my clipboard".
        """
        if not text:
            return "Nothing to copy — no text was provided, sir."

        preview = text if len(text) <= 60 else text[:57] + "..."
        if not plat.IS_WINDOWS:
            if not plat.clipboard_set(text):
                return "I was unable to write to the clipboard, sir."
            return f"Copied to clipboard: \"{preview}\""

        if not _open_clipboard():
            return "I couldn't access the clipboard — another application may be holding it. Please try again in a moment, sir."

        try:
            user32.EmptyClipboard()

            # Allocate global memory for the text (UTF-16, includes null terminator)
            encoded = (text + "\0").encode("utf-16-le")
            byte_count = len(encoded)

            GMEM_MOVEABLE = 0x0002
            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, byte_count)
            if not h_mem:
                return "I ran out of memory while trying to copy to the clipboard, sir."

            ptr = kernel32.GlobalLock(h_mem)
            if not ptr:
                kernel32.GlobalFree(h_mem)
                return "I was unable to write to the clipboard, sir."

            try:
                ctypes.memmove(ptr, encoded, byte_count)
            finally:
                kernel32.GlobalUnlock(h_mem)

            result = user32.SetClipboardData(CF_UNICODETEXT, h_mem)
            if not result:
                kernel32.GlobalFree(h_mem)
                return "I failed to set the clipboard data, sir."

        finally:
            user32.CloseClipboard()

        # Provide a brief preview so the user knows what was copied
        return f"Copied to clipboard: \"{preview}\""
