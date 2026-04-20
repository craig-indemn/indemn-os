import { useCallback, useRef } from "react";

interface Props {
  onResize: (deltaX: number) => void;
}

export function ResizeDivider({ onResize }: Props) {
  const lastX = useRef(0);
  const onResizeRef = useRef(onResize);
  onResizeRef.current = onResize;

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    lastX.current = e.clientX;
    e.preventDefault();

    const handleMouseMove = (ev: MouseEvent) => {
      const delta = ev.clientX - lastX.current;
      lastX.current = ev.clientX;
      onResizeRef.current(-delta);
    };

    const handleMouseUp = () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  return (
    <div
      onMouseDown={handleMouseDown}
      className="w-2 bg-gray-200 hover:bg-blue-400 cursor-col-resize flex-shrink-0 transition-colors"
      title="Drag to resize"
    />
  );
}
