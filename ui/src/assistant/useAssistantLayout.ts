import { useState, useCallback } from "react";

const OPEN_KEY = "assistant_pane_open";
const WIDTH_KEY = "assistant_pane_width";
const DEFAULT_WIDTH = 420;
const MIN_WIDTH = 300;
const MAX_WIDTH_RATIO = 0.6;

export function useAssistantLayout() {
  const [isOpen, setIsOpen] = useState(() => {
    try { return localStorage.getItem(OPEN_KEY) === "true"; }
    catch { return false; }
  });

  const [width, setWidthState] = useState(() => {
    try {
      const stored = localStorage.getItem(WIDTH_KEY);
      return stored ? Number(stored) : DEFAULT_WIDTH;
    } catch { return DEFAULT_WIDTH; }
  });

  const setWidth = useCallback((w: number) => {
    const maxW = window.innerWidth * MAX_WIDTH_RATIO;
    const clamped = Math.max(MIN_WIDTH, Math.min(w, maxW));
    setWidthState(clamped);
    localStorage.setItem(WIDTH_KEY, String(clamped));
  }, []);

  const toggleOpen = useCallback(() => {
    setIsOpen((prev) => {
      const next = !prev;
      localStorage.setItem(OPEN_KEY, String(next));
      return next;
    });
  }, []);

  const openPane = useCallback(() => {
    if (!isOpen) {
      setIsOpen(true);
      localStorage.setItem(OPEN_KEY, "true");
    }
  }, [isOpen]);

  return { isOpen, width, setWidth, toggleOpen, openPane };
}
