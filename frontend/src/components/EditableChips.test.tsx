import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import i18n from "../i18n";
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

describe("EditableChips localization (F1 / M8.3)", () => {
  afterEach(async () => {
    await i18n.changeLanguage("en"); // restore the shared singleton
  });

  it("shows closed-vocabulary chips in the UI language but keeps free-form chips as-is", async () => {
    await i18n.changeLanguage("fr");
    render(
      <EditableChips
        label="Avoiding"
        tone="avoid"
        items={["Horror", "gory", "gripping true-crime docs"]}
        busy={false}
        onAdd={() => {}}
        onRemove={() => {}}
      />,
    );
    // Vocabulary terms are translated for display...
    expect(screen.getByText("Horreur")).toBeInTheDocument();
    expect(screen.getByText("gore")).toBeInTheDocument();
    // ...but a free-form phrase outside the vocabulary is shown verbatim.
    expect(screen.getByText("gripping true-crime docs")).toBeInTheDocument();
    expect(screen.queryByText("Horror")).not.toBeInTheDocument();
  });

  it("removes with the canonical English value even when the chip is shown translated", async () => {
    await i18n.changeLanguage("fr");
    const onRemove = vi.fn();
    render(
      <EditableChips
        label="Avoiding"
        tone="avoid"
        items={["Horror"]}
        busy={false}
        onAdd={() => {}}
        onRemove={onRemove}
      />,
    );
    // The remove control is labelled (in French) with the translated text the user sees...
    fireEvent.click(screen.getByLabelText("Retirer Horreur"));
    // ...but the value written back up (the override key sent to the backend) stays English.
    expect(onRemove).toHaveBeenCalledWith("Horror");
  });

  it("keeps the stored value stable across an EN→FR→EN language switch (override survives)", async () => {
    const onRemove = vi.fn();
    const props = {
      label: "Avoiding",
      tone: "avoid" as const,
      items: ["Science Fiction"],
      busy: false,
      onAdd: () => {},
      onRemove,
    };
    await i18n.changeLanguage("en");
    const { rerender } = render(<EditableChips {...props} />);
    expect(screen.getByText("Science Fiction")).toBeInTheDocument();

    await i18n.changeLanguage("fr");
    rerender(<EditableChips {...props} />);
    expect(screen.getByText("Science-Fiction")).toBeInTheDocument();

    await i18n.changeLanguage("en");
    rerender(<EditableChips {...props} />);
    expect(screen.getByText("Science Fiction")).toBeInTheDocument();

    // Whatever the display language, removing yields the same canonical English override key.
    fireEvent.click(screen.getByLabelText("Remove Science Fiction"));
    expect(onRemove).toHaveBeenCalledWith("Science Fiction");
  });
});
