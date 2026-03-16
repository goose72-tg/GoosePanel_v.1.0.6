# Goose Panel

**Goose Panel** is a panel for launching multiple CS2 and automation farming case CS2.

Created in 2 months using Chat GPT.


## 📌 Requirements

| Requirement   | Note                      |
| ------------- | ------------------------- |
| Python 3.13   | Required to run the panel |
| Steam         | Latest version            |
| CS2           | Latest version            |
| Handle.exe    | utility with google       |

**Handle.exe** - https://learn.microsoft.com/ru-ru/sysinternals/downloads/handle

> ⚠️ Important: Make sure `cmd` always runs as administrator for proper functionality.

---

## 🛠 Installation


1. Install dependencies:

* win - cmd "run as administrator"
* cd {path to folder}
* pip install -r requirements.txt

2. Run the panel:

* cd {insert path to folder}
* py main.py

---
## 🛠 Troubleshooting



### Accounts or games fail to launch/accept
**Issue:** The panel does not trigger the game client, or accounts do not start at all.
**Solution:** The panel must be run with **Administrator privileges**.

### How to compile into an .exe file?
1. Open **Command Prompt (CMD)** as **Administrator**.
2. cd {path to folder}
3. pyinstaller --onefile --noconsole --clean --name "GoosePanel" --icon=Icon1.ico --add-data "Icon1.ico;." --uac-admin main.py

---

## ⚙ Account Setup
1. To add accounts, place your `maFiles` (optional) in the `mafiles` folder.
2. Add logins and passwords in the `logpass.txt` file in the format:

```
login:password
```

---

## 🚀 Usage

* The panel allows launching multiple CS2 accounts simultaneously.
* Automatically arranges windows and collects lobbies.
* Works with accounts listed in `logpass.txt` and `maFiles`.


### Functional

* The panel automatically farms keys in cs2, but does not automatically collect items.

### Drop stats

* A drop report was implemented on the history of the sent trade.
