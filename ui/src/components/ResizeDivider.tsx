import { useCallback, useRef } from "react";

interface Props {
  onResize: (deltaX: number) => void;
}

export function ResizeDivider({ onResize }: Props) {
  const dragging = useRef(false);
  const lastX = useRef(0);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    dragging.current = true;
    lastX.current = e.clientX;
    e.preventDefault();

    const handleMouseMove = (ev: MouseEvent) => {
      if (!dragging.current) return;
      const delta = ev.clientX - lastX.current;
      lastX.current = ev.clientX;
      onResize(-delta); // negative because dragging left increases pane width
    };

    const handleMouseUp = () => {
      dragging.current = false;
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, [onResize]);

  return (
    <div
      onMouseDown={handleMouseDown}
      className="w-1.5 bg-gray-200 hover:bg-blue-300 cursor-col-resize flex-shrink-0 transition-colors"
    />
  );
}
