import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EditableChips } from "./EditableChips";

describe("EditableChips", () => {
  it("renders each item and removes on click", () => {
    const onRemove = vi.fn();
    render(
      <EditableChips
        label="Avoiding"
        tone="avoid"
        items={["gore", "musicals"]}
        busy={false}
        onAdd={() => {}}
        onRemove={onRemove}
      />,
    );
    expect(screen.getAllByTestId("taste-chip")).toHaveLength(2);
    fireEvent.click(screen.getByLabelText("Remove gore"));
    expect(onRemove).toHaveBeenCalledWith("gore");
  });

  it("adds a trimmed value on Enter and ignores duplicates", () => {
    const onAdd = vi.fn();
    render(
      <EditableChips
        label="Drawn to"
        tone="like"
        items={["sci-fi"]}
        busy={false}
        onAdd={onAdd}
        onRemove={() => {}}
      />,
    );
    const input = screen.getByTestId("taste-add-like");
    fireEvent.change(input, { target: { value: "  drama  " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAdd).toHaveBeenCalledWith("drama");

    fireEvent.change(input, { target: { value: "sci-fi" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAdd).toHaveBeenCalledTimes(1); // duplicate ignored
  });
});
