/**
 * Copyright 2017-2021, Voxel51, Inc.
 */

import { BaseState, Coordinates } from "../state";
import { clampScale } from "../util";
import { BaseElement, Events } from "./base";
import { ICONS, makeCheckboxRow, makeWrapper } from "./util";

export class LookerElement<State extends BaseState> extends BaseElement<
  State,
  HTMLDivElement
> {
  private hideControlsTimeout?: ReturnType<typeof setTimeout>;
  private start: Coordinates = [0, 0];
  private wheelTimeout: ReturnType<typeof setTimeout>;

  getEvents(): Events<State> {
    return {
      keydown: ({ event, update, dispatchEvent }) => {
        const e = event as KeyboardEvent;
        switch (e.key) {
          case "ArrowDown":
            update(
              ({ rotate }) => ({ rotate: rotate + 1 }),
              dispatchTooltipEvent(dispatchEvent)
            );
            return;
          case "ArrowUp":
            update(
              ({ rotate }) => ({ rotate: Math.max(rotate - 1, 0) }),
              dispatchTooltipEvent(dispatchEvent)
            );
            return;
          case "Escape":
            update({ showControls: false, showOptions: false });
            return;
          case "s":
            update((state) => ({
              showOptions: state.showOptions,
              showControls: state.showControls,
            }));
            return;
        }
      },
      mouseenter: ({ update, dispatchEvent }) => {
        dispatchEvent("mouseenter");
        update(({ config: { thumbnail } }) => {
          if (thumbnail) {
            return { hovering: true };
          }
          return {
            hovering: true,
            showControls: true,
          };
        });
      },
      mouseleave: ({ update, dispatchEvent }) => {
        dispatchEvent("mouseleave");
        update({
          hovering: false,
          disableControls: false,
          panning: false,
        });
      },
      mousedown: ({ event, update }) => {
        update(({ config: { thumbnail }, pan: [x, y] }) => {
          if (thumbnail) {
            return {};
          }
          event.preventDefault();
          this.start = [event.pageX - x, event.pageY - y];
          return { panning: true, canZoom: false };
        });
      },
      mouseup: ({ event, update }) => {
        update((state) => {
          if (state.config.thumbnail || !state.panning) {
            return {};
          }
          event.preventDefault();
          return {
            panning: false,
            pan: this.getPan([event.pageX, event.pageY]),
          };
        });
      },
      mousemove: ({ event, update }) => {
        if (this.hideControlsTimeout) {
          clearTimeout(this.hideControlsTimeout);
        }
        this.hideControlsTimeout = setTimeout(
          () =>
            update(({ showOptions }) => {
              this.hideControlsTimeout = null;
              if (!showOptions) {
                return { showControls: false };
              }
              return {};
            }),
          2500
        );
        update((state) => {
          if (state.config.thumbnail || !state.panning) {
            return state.rotate !== 0 ? { rotate: 0 } : {};
          }
          return {
            rotate: 0,
            pan: this.getPan([event.pageX, event.pageY]),
          };
        });
      },
      dblclick: ({ update }) => {
        update(({ config: { thumbnail } }) => {
          return thumbnail ? {} : { scale: 1, pan: [0, 0], canZoom: true };
        });
      },
      wheel: ({ event, update, dispatchEvent }) => {
        update(
          ({
            config: { thumbnail, dimensions },
            pan: [px, py],
            scale,
            windowBBox: [tlx, tly, width, height],
          }) => {
            if (thumbnail) {
              return {};
            }
            event.preventDefault();

            const x = event.x - tlx;
            const y = event.y - tly;

            const xs = (x - px) / scale;
            const ys = (y - py) / scale;
            const newScale = clampScale(
              [width, height],
              dimensions,
              event.deltaY < 0 ? scale * 1.15 : scale / 1.15
            );

            if (scale === newScale) {
              return {};
            }

            if (this.wheelTimeout) {
              clearTimeout(this.wheelTimeout);
            }

            this.wheelTimeout = setTimeout(() => {
              this.wheelTimeout = null;
              update({ wheeling: false });
            }, 200);

            return {
              pan: [x - xs * newScale, y - ys * newScale],
              scale: newScale,
              canZoom: false,
              cursorCoordinates: [
                (<MouseEvent>event).pageX,
                (<MouseEvent>event).pageY,
              ],
              wheeling: true,
            };
          },
          dispatchTooltipEvent(dispatchEvent)
        );
      },
    };
  }

  createHTMLElement() {
    const element = document.createElement("div");
    element.className = "looker loading";
    element.tabIndex = -1;
    return element;
  }

  renderSelf({ loaded, hovering, config: { thumbnail } }) {
    if (loaded && this.element.classList.contains("loading")) {
      this.element.classList.remove("loading");
    }
    if (!thumbnail && hovering && this.element !== document.activeElement) {
      this.element.focus();
    }

    return this.element;
  }

  private getPan([x, y]: Coordinates): Coordinates {
    const [sx, sy] = this.start;
    return [x - sx, y - sy];
  }
}

export class CanvasElement<State extends BaseState> extends BaseElement<
  State,
  HTMLCanvasElement
> {
  private width: number;
  private height: number;

  getEvents(): Events<State> {
    return {
      click: ({ update, dispatchEvent }) => {
        update({ showOptions: false }, (state, overlays) => {
          if (!state.config.thumbnail && overlays.length) {
            dispatchEvent("select", overlays[0].getSelectData(state));
          }
        });
      },
      mouseleave: ({ dispatchEvent }) => {
        dispatchEvent("tooltip", null);
      },
      mousemove: ({ event, update, dispatchEvent }) => {
        update((state) => {
          return state.config.thumbnail
            ? {}
            : {
                cursorCoordinates: [
                  (<MouseEvent>event).pageX,
                  (<MouseEvent>event).pageY,
                ],
              };
        }, dispatchTooltipEvent(dispatchEvent));
      },
    };
  }

  createHTMLElement() {
    const element = document.createElement("canvas");
    return element;
  }

  renderSelf({
    config: { thumbnail },
    panning,
    windowBBox: [_, __, width, height],
  }: Readonly<State>) {
    if (this.width !== width) {
      this.element.width = width;
    }
    if (this.height !== height) {
      this.element.height = height;
    }
    if (panning && this.element.style.cursor !== "all-scroll") {
      this.element.style.cursor = "all-scroll";
    } else if (
      !thumbnail &&
      !panning &&
      this.element.style.cursor !== "default"
    ) {
      this.element.style.cursor = "default";
    }
    return this.element;
  }
}

export class ControlsElement<State extends BaseState> extends BaseElement<
  State
> {
  private showControls: boolean;

  getEvents(): Events<State> {
    return {
      click: ({ event, update }) => {
        event.stopPropagation();
        update({
          showControls: false,
          disableControls: true,
          showOptions: false,
        });
      },
      mouseenter: ({ update }) => {
        update({ hoveringControls: true });
      },
      mouseleave: ({ update }) => {
        update({ hoveringControls: false });
      },
      wheel: ({ event }) => {
        event.preventDefault();
        event.stopPropagation();
      },
      dblclick: ({ event }) => {
        event.preventDefault();
        event.stopPropagation();
      },
      mousedown: ({ event }) => {
        event.preventDefault();
        event.stopPropagation();
      },
      mouseup: ({ event }) => {
        event.preventDefault();
        event.stopPropagation();
      },
    };
  }

  createHTMLElement() {
    const element = document.createElement("div");
    element.className = "looker-controls";
    return element;
  }

  isShown({ config: { thumbnail } }) {
    return !thumbnail;
  }

  renderSelf({ showControls, disableControls, config: { thumbnail } }) {
    if (thumbnail) {
      return this.element;
    }
    showControls = showControls && !disableControls;
    if (this.showControls === showControls) {
      return this.element;
    }
    if (showControls) {
      this.element.style.opacity = "0.9";
      this.element.style.height = "unset";
    } else {
      this.element.style.opacity = "0.0";
      this.element.style.height = "0";
    }
    this.showControls = showControls;
    return this.element;
  }
}

export class FullscreenButtonElement<
  State extends BaseState
> extends BaseElement<State, HTMLImageElement> {
  private fullscreen: boolean;

  getEvents(): Events<State> {
    return {
      click: ({ event, update }) => {
        event.stopPropagation();
        update(({ fullscreen }) => ({ fullscreen: !fullscreen }));
      },
    };
  }

  createHTMLElement() {
    const element = document.createElement("img");
    element.className = "looker-clickable";
    element.style.gridArea = "2 / 5 / 2 / 5";
    return element;
  }

  renderSelf({ fullscreen }) {
    if (this.fullscreen !== fullscreen) {
      this.fullscreen = fullscreen;
      this.element.src = fullscreen ? ICONS.fullscreenExit : ICONS.fullscreen;
      this.element.title = `${fullscreen ? "Minimize" : "Maximize"} (m)`;
    }
    return this.element;
  }
}

export class PlusElement<State extends BaseState> extends BaseElement<
  State,
  HTMLImageElement
> {
  getEvents(): Events<State> {
    return {
      click: ({ event, update }) => {
        event.stopPropagation();
        // update(({ fullscreen }) => ({ fullscreen: !fullscreen }));
      },
    };
  }

  createHTMLElement() {
    const element = document.createElement("img");
    element.className = "looker-clickable";
    element.src = ICONS.plus;
    element.title = "Zoom in (+)";
    element.style.gridArea = "2 / 5 / 2 / 5";
    return element;
  }

  renderSelf() {
    return this.element;
  }
}

export class MinusElement<State extends BaseState> extends BaseElement<
  State,
  HTMLImageElement
> {
  getEvents(): Events<State> {
    return {
      click: ({ event, update }) => {
        event.stopPropagation();
        // update(({ fullscreen }) => ({ fullscreen: !fullscreen }));
      },
    };
  }

  createHTMLElement() {
    const element = document.createElement("img");
    element.className = "looker-clickable";
    element.src = ICONS.minus;
    element.title = "Zoom out (-)";
    element.style.gridArea = "2 / 5 / 2 / 5";
    return element;
  }

  renderSelf() {
    return this.element;
  }
}

export class OptionsButtonElement<State extends BaseState> extends BaseElement<
  State
> {
  getEvents(): Events<State> {
    return {
      click: ({ event, update }) => {
        event.stopPropagation();
        update((state) => ({ showOptions: !state.showOptions }));
      },
    };
  }

  createHTMLElement() {
    const element = document.createElement("img");
    element.className = "looker-clickable";
    element.src = ICONS.options;
    element.title = "Settings (s)";
    element.style.gridArea = "2 / 5 / 2 / 5";
    return element;
  }

  renderSelf() {
    return this.element;
  }
}

export class OptionsPanelElement<State extends BaseState> extends BaseElement<
  State
> {
  private showOptions: boolean;
  getEvents(): Events<State> {
    return {
      click: ({ event }) => {
        event.stopPropagation();
        event.preventDefault();
      },
      dblclick: ({ event }) => {
        event.stopPropagation();
        event.preventDefault();
      },
      mouseenter: ({ update }) => {
        update({ hoveringControls: true });
      },
      mouseleave: ({ update }) => {
        update({ hoveringControls: false });
      },
    };
  }

  createHTMLElement() {
    const element = document.createElement("div");
    element.className = "looker-options-panel";
    return element;
  }

  isShown({ config: { thumbnail } }) {
    return !thumbnail;
  }

  renderSelf({ showOptions, config: { thumbnail } }) {
    if (thumbnail) {
      return this.element;
    }
    if (this.showOptions === showOptions) {
      return this.element;
    }
    if (showOptions) {
      this.element.style.opacity = "0.9";
      this.element.classList.remove("looker-display-none");
    } else {
      this.element.style.opacity = "0.0";
      this.element.classList.add("looker-display-none");
    }
    this.showOptions = showOptions;
    return this.element;
  }
}

export class OnlyShowHoveredOnLabelOptionElement<
  State extends BaseState
> extends BaseElement<State> {
  checkbox: HTMLInputElement;
  label: HTMLLabelElement;

  getEvents(): Events<State> {
    return {
      click: ({ event, update }) => {
        event.stopPropagation();
        event.preventDefault();
        update(({ options: { onlyShowHoveredLabel } }) => ({
          options: { onlyShowHoveredLabel: !onlyShowHoveredLabel },
        }));
      },
    };
  }

  createHTMLElement() {
    [this.label, this.checkbox] = makeCheckboxRow(
      "Only show hovered label",
      false
    );
    return makeWrapper([this.label]);
  }

  renderSelf({ options: { onlyShowHoveredLabel } }) {
    this.checkbox.checked = onlyShowHoveredLabel;
    return this.element;
  }
}

export class ShowLabelOptionElement<
  State extends BaseState
> extends BaseElement<State> {
  checkbox: HTMLInputElement;
  label: HTMLLabelElement;

  getEvents(): Events<State> {
    return {
      click: ({ event, update, dispatchEvent }) => {
        event.stopPropagation();
        event.preventDefault();
        update(({ options: { showLabel } }) => {
          dispatchEvent("options", { showLabel: !showLabel });
          return {
            options: { showLabel: !showLabel },
          };
        });
      },
    };
  }

  createHTMLElement() {
    [this.label, this.checkbox] = makeCheckboxRow("Show label", false);
    return makeWrapper([this.label]);
  }

  renderSelf({ options: { showLabel } }) {
    this.checkbox.checked = showLabel;
    return this.element;
  }
}

export class ShowConfidenceOptionElement<
  State extends BaseState
> extends BaseElement<State> {
  checkbox: HTMLInputElement;
  label: HTMLLabelElement;

  getEvents(): Events<State> {
    return {
      click: ({ event, update, dispatchEvent }) => {
        event.stopPropagation();
        event.preventDefault();
        update(({ options: { showConfidence } }) => {
          dispatchEvent("options", { showConfidence: !showConfidence });
          return {
            options: { showConfidence: !showConfidence },
          };
        });
      },
    };
  }

  createHTMLElement() {
    [this.label, this.checkbox] = makeCheckboxRow("Show confidence", false);
    return makeWrapper([this.label]);
  }

  renderSelf({ options: { showConfidence } }) {
    this.checkbox.checked = showConfidence;
    return this.element;
  }
}

export class ShowTooltipOptionElement<
  State extends BaseState
> extends BaseElement<State> {
  checkbox: HTMLInputElement;
  label: HTMLLabelElement;

  getEvents(): Events<State> {
    return {
      click: ({ event, update, dispatchEvent }) => {
        event.stopPropagation();
        event.preventDefault();
        update(({ options: { showTooltip } }) => {
          dispatchEvent("options", { showTooltip: !showTooltip });
          return {
            options: { showTooltip: !showTooltip },
          };
        });
      },
    };
  }

  createHTMLElement() {
    [this.label, this.checkbox] = makeCheckboxRow("Show tooltip", false);
    return makeWrapper([this.label]);
  }

  renderSelf({ options: { showTooltip } }) {
    this.checkbox.checked = showTooltip;
    return this.element;
  }
}

export const transformWindowElement = (
  { pan: [x, y], scale }: Readonly<BaseState>,
  element: HTMLElement
): void => {
  element.style.transform =
    "translate3d(" +
    Math.round(x) +
    "px, " +
    Math.round(y) +
    "px, 0px) scale(" +
    scale +
    ")";
};

const dispatchTooltipEvent = (dispatchEvent) => {
  return (state, overlays) => {
    // @ts-ignore
    if (state.playing && state.config.thumbnail) {
      return;
    }
    if (!state.options.showTooltip) {
      return;
    }
    let detail =
      overlays.length && overlays[0].containsPoint(state)
        ? overlays[0].getPointInfo(state)
        : null;
    // @ts-ignore
    if (state.frameNumber && detail) {
      // @ts-ignore
      detail.frameNumber = state.frameNumber;
    }
    dispatchEvent(
      "tooltip",
      detail
        ? {
            ...detail,
            coordinates: state.cursorCoordinates,
          }
        : null
    );
  };
};
