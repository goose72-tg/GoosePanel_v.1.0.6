from datetime import datetime


class LogManager:
    _instance = None
    TIMESTAMP_TAG = "log_timestamp"

    def __new__(cls, textbox=None):
        if cls._instance is None:
            cls._instance = super(LogManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, textbox=None):
        if self._initialized:
            return

        if textbox is not None:
            #raise ValueError("Textbox must be provided for the first initialization")
            self.textbox = textbox
            self.textbox.configure(state="normal")
            self.textbox.delete("0.0", "end")
            self._configure_tags()
            self.textbox.configure(state="disabled")
            self._initialized = True

    def _configure_tags(self):
        tk_textbox = getattr(self.textbox, "_textbox", self.textbox)
        if hasattr(tk_textbox, "tag_config"):
            tk_textbox.tag_config(self.TIMESTAMP_TAG, foreground="#ffd54f")
        elif hasattr(tk_textbox, "tag_configure"):
            tk_textbox.tag_configure(self.TIMESTAMP_TAG, foreground="#ffd54f")

    def add_log(self, message):
        timestamp = datetime.now().strftime("[%H:%M:%S]")
        self._configure_tags()
        self.textbox.configure(state="normal")
        self.textbox.insert("end", timestamp, self.TIMESTAMP_TAG)
        self.textbox.insert("end", f" {message}\n")
        self.textbox.see("end")  # прокрутка вниз
        self.textbox.configure(state="disabled")