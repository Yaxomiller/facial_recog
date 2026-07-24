import { useEffect, useRef, useState } from "react";

// In-app touchscreen keyboard. Appears whenever a text field is focused and
// types into it via the native value setter so React's controlled inputs
// update exactly as if a physical keyboard was used. Built for the Radxa
// touchscreen kiosk where no hardware keyboard is attached.

const IGNORED_INPUT_TYPES = new Set([
  "checkbox",
  "radio",
  "button",
  "submit",
  "reset",
  "file",
  "range",
  "color",
  "date",
  "time",
]);

const LAYOUTS = {
  default: [
    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
    ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
    ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
    ["shift", "z", "x", "c", "v", "b", "n", "m", "backspace"],
    ["symbols", "@", "space", ".", "done"],
  ],
  shift: [
    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
    ["Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P"],
    ["A", "S", "D", "F", "G", "H", "J", "K", "L"],
    ["shift", "Z", "X", "C", "V", "B", "N", "M", "backspace"],
    ["symbols", "@", "space", ".", "done"],
  ],
  symbols: [
    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
    ["!", "@", "#", "$", "%", "^", "&", "*", "(", ")"],
    ["-", "_", "=", "+", "[", "]", "{", "}", ";"],
    [":", "'", "\"", ",", ".", "/", "?", "backspace"],
    ["abc", "space", "done"],
  ],
};

const KEY_LABELS = {
  shift: "⇧",
  backspace: "⌫",
  space: "space",
  done: "Done",
  symbols: "?123",
  abc: "ABC",
};

function setNativeValue(input, value) {
  const prototype = input.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  if (setter) {
    setter.call(input, value);
  } else {
    input.value = value;
  }
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function readSelection(input) {
  try {
    if (input.selectionStart == null || input.selectionEnd == null) {
      return null;
    }
    return { start: input.selectionStart, end: input.selectionEnd };
  } catch (_error) {
    // Types like email/number do not support selection APIs.
    return null;
  }
}

function insertText(input, text) {
  const selection = readSelection(input);
  if (!selection) {
    setNativeValue(input, input.value + text);
    return;
  }
  const { start, end } = selection;
  const nextValue = input.value.slice(0, start) + text + input.value.slice(end);
  setNativeValue(input, nextValue);
  const caret = start + text.length;
  try {
    input.setSelectionRange(caret, caret);
  } catch (_error) {
    // Ignore for inputs that do not support caret positioning.
  }
}

function deleteBackward(input) {
  const selection = readSelection(input);
  if (!selection) {
    setNativeValue(input, input.value.slice(0, -1));
    return;
  }
  const { start, end } = selection;
  if (start === end) {
    if (start === 0) {
      return;
    }
    const nextValue = input.value.slice(0, start - 1) + input.value.slice(end);
    setNativeValue(input, nextValue);
    try {
      input.setSelectionRange(start - 1, start - 1);
    } catch (_error) {
      // Ignore.
    }
    return;
  }
  const nextValue = input.value.slice(0, start) + input.value.slice(end);
  setNativeValue(input, nextValue);
  try {
    input.setSelectionRange(start, start);
  } catch (_error) {
    // Ignore.
  }
}

export default function OnScreenKeyboard() {
  const [visible, setVisible] = useState(false);
  const [layout, setLayout] = useState("default");
  const activeInputRef = useRef(null);
  const hideTimerRef = useRef(null);

  useEffect(() => {
    function isTypeableInput(element) {
      if (!element) {
        return false;
      }
      const tag = element.tagName;
      if (tag === "TEXTAREA") {
        return true;
      }
      if (tag !== "INPUT") {
        return false;
      }
      const type = (element.getAttribute("type") || "text").toLowerCase();
      return !IGNORED_INPUT_TYPES.has(type);
    }

    function handleFocusIn(event) {
      if (isTypeableInput(event.target)) {
        if (hideTimerRef.current) {
          window.clearTimeout(hideTimerRef.current);
          hideTimerRef.current = null;
        }
        activeInputRef.current = event.target;
        setLayout("default");
        setVisible(true);
        window.setTimeout(() => {
          try {
            event.target.scrollIntoView({ block: "center", behavior: "smooth" });
          } catch (_error) {
            // Ignore browsers without smooth scroll options.
          }
        }, 60);
      }
    }

    function handleFocusOut() {
      // Delay so a key press (which keeps the input focused via preventDefault)
      // never flickers the keyboard closed. Only hide when focus truly left
      // every text field.
      hideTimerRef.current = window.setTimeout(() => {
        const active = document.activeElement;
        if (!isTypeableInput(active)) {
          setVisible(false);
          activeInputRef.current = null;
        }
      }, 200);
    }

    document.addEventListener("focusin", handleFocusIn);
    document.addEventListener("focusout", handleFocusOut);
    return () => {
      document.removeEventListener("focusin", handleFocusIn);
      document.removeEventListener("focusout", handleFocusOut);
      if (hideTimerRef.current) {
        window.clearTimeout(hideTimerRef.current);
      }
    };
  }, []);

  function pressKey(key) {
    const input = activeInputRef.current;
    if (!input) {
      return;
    }

    switch (key) {
      case "shift":
        setLayout((current) => (current === "shift" ? "default" : "shift"));
        return;
      case "symbols":
        setLayout("symbols");
        return;
      case "abc":
        setLayout("default");
        return;
      case "backspace":
        deleteBackward(input);
        return;
      case "space":
        insertText(input, " ");
        return;
      case "done":
        setVisible(false);
        activeInputRef.current = null;
        input.blur();
        return;
      default:
        insertText(input, key);
        if (layout === "shift") {
          setLayout("default");
        }
    }
  }

  if (!visible) {
    return null;
  }

  const rows = LAYOUTS[layout] || LAYOUTS.default;

  return (
    <div className="osk" role="group" aria-label="On-screen keyboard">
      {rows.map((row, rowIndex) => (
        <div className="osk-row" key={rowIndex}>
          {row.map((key) => (
            <button
              key={key}
              type="button"
              className={`osk-key osk-key-${key.length > 1 ? key : "char"}`}
              // preventDefault on pointer/mouse down keeps the input focused
              // so the caret stays put; the click handler does the typing.
              onPointerDown={(event) => event.preventDefault()}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => pressKey(key)}
            >
              {KEY_LABELS[key] || key}
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}
