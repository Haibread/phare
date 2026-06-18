import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type AvailabilityCtx, AvailabilityProvider, TitleAction } from "./Availability";

function renderAction(ctx: Partial<AvailabilityCtx>, titleId = "t1") {
  const value: AvailabilityCtx = {
    configured: true,
    results: {},
    requestingId: null,
    onRequest: () => {},
    ...ctx,
  };
  return render(
    <AvailabilityProvider value={value}>
      <TitleAction titleId={titleId} />
    </AvailabilityProvider>,
  );
}

describe("TitleAction", () => {
  it("offers a Request button for requestable titles and calls back", () => {
    const onRequest = vi.fn();
    renderAction({ results: { t1: "requestable" }, onRequest });
    fireEvent.click(screen.getByTestId("title-request"));
    expect(onRequest).toHaveBeenCalledWith("t1");
  });

  it("shows Available / Queued for those states", () => {
    renderAction({ results: { t1: "available" } });
    expect(screen.getByTestId("title-available")).toBeInTheDocument();

    renderAction({ results: { t1: "queued" } }, "t1");
    expect(screen.getByTestId("title-queued")).toBeInTheDocument();
  });

  it("renders nothing when Seerr isn't configured or the title is unknown", () => {
    const { container: a } = renderAction({ configured: false, results: { t1: "requestable" } });
    expect(a).toBeEmptyDOMElement();
    const { container: b } = renderAction({ results: { t1: "unknown" } });
    expect(b).toBeEmptyDOMElement();
  });
});
