// Supply chain visibility map.
//
// One module for all three map contexts — the operational map, a shipment journey
// and a container journey — because they draw the same GeoJSON contract and differ
// only in which layers they add.
//
// Three things this module deliberately does not do:
//
//   * It never decides what a position means. Whether a marker is a physical
//     location, carrier evidence or a destination, how old it is, and every label
//     come from Container SCM as feature properties. In particular the precedence
//     physical > tracking > nothing is decided server-side; re-deriving any of it
//     here would give the platform two answers to the same question.
//
//   * It never separates markers that share a coordinate. Containers standing at
//     one terminal arrive already grouped, carrying a count. Jittering them apart
//     would draw a precision the data does not have: the coordinate belongs to the
//     terminal, not to the box somewhere inside it.
//
//   * It never rebuilds the map. Filters replace the data in the existing GeoJSON
//     source, so panning and zoom survive a filter change.
//
// Mapbox is an enhancement. Without a token the page renders its configuration
// notice, this module finds no map element to initialise, and nothing throws.

import mapboxgl from 'mapbox-gl';
import 'mapbox-gl/dist/mapbox-gl.css';

const SOURCE_ID = 'scm-visibility';
// A world view, not a home port. Centring an empty map on Gothenburg would imply
// activity there; this implies nothing.
const FALLBACK_CENTER = [10, 35];
const FALLBACK_ZOOM = 1.4;
const FIT_PADDING = 56;
const MAX_FIT_ZOOM = 9;

// Semantic colours, mirroring the roles DaisyUI uses for the same meanings.
// Fixed hex rather than theme variables because Mapbox GL cannot parse the
// oklch() values the theme exposes.
//
// Colour is never the only difference between two position classes — each has its
// own marker shape as well, so the map survives greyscale printing and the common
// forms of colour blindness. See makeMarkerImage.
const COLOR = {
  physical: '#16a34a',
  tracking: '#0284c7',
  destination: '#f59e0b',
  actual: '#0f766e',
  forecast: '#6366f1',
  selected: '#0284c7',
  ink: '#0f172a',
};

// Marker shape per position class. The shape carries the meaning; the colour only
// reinforces it.
//
//   physical      ● a filled disc      — accepted, the strongest claim
//   tracking      ◇ a hollow diamond   — evidence, deliberately lighter
//   destination   ▣ a hollow square    — an intention, not a position
const MARKER_SHAPE = {
  physical: 'disc',
  tracking: 'diamond',
  destination: 'square',
};

// Weakest claim first. The destination square is drawn largest and lowest so that
// a physical disc landing on the same terminal sits inside it and both stay
// readable — which is the honest picture when boxes are there and more are coming.
const DRAW_ORDER = ['destination', 'tracking', 'physical'];

const MARKER_SIZE = {
  physical: 26,
  tracking: 28,
  destination: 38,
};

// White on the filled disc, ink on the hollow shapes. Readability, not decoration.
const MARKER_TEXT_COLOR = [
  'match',
  ['get', 'position_class'],
  'physical', '#ffffff',
  COLOR.ink,
];

const IS_POSITION = ['==', ['get', 'object_type'], 'map_position'];
const IS_EVENT = ['==', ['get', 'object_type'], 'event'];

const EMPTY = { type: 'FeatureCollection', features: [] };

class VisibilityMap {
  constructor(element) {
    this.element = element;
    this.mode = element.dataset.mapMode || 'overview';
    // The URL describing the current selection, without the map's own view state.
    // Kept apart from dataUrl so a board swap can replace the selection while the
    // legend's toggles keep applying.
    this.baseUrl = element.dataset.mapDataUrl || '';
    this.dataUrl = this.baseUrl;
    this.panelSelector = element.dataset.mapPanelTarget || '';
    this.selectedEventId = null;
    this.data = EMPTY;

    mapboxgl.accessToken = element.dataset.mapboxToken;
    this.map = new mapboxgl.Map({
      container: element,
      style: element.dataset.mapboxStyle,
      center: FALLBACK_CENTER,
      zoom: FALLBACK_ZOOM,
      cooperativeGestures: true,
    });
    this.map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), 'top-right');
    this.map.on('load', () => this.onLoad());
  }

  onLoad() {
    this.map.addSource(SOURCE_ID, {
      type: 'geojson',
      data: EMPTY,
      // No Mapbox clustering. Containers at one canonical location are grouped
      // server-side, by that location, which is the grouping that means something:
      // "84 at Oceanterminalen" rather than "84 within 44 screen pixels".
      cluster: false,
    });
    // Journey layers first so the canonical markers draw on top of the evidence.
    if (this.mode !== 'overview') this.addJourneyLayers();
    this.addPositionLayers();
    this.map.resize();
    this.refresh(withMapFilters(this.baseUrl));
  }

  addPositionLayers() {
    // One layer per class, added weakest first, so a physical disc draws on top of
    // a destination square at the same terminal and the square still shows around
    // it. Two markers on one coordinate is the truth — some boxes are there, more
    // are coming — and nudging them apart would draw a distance that is not real.
    DRAW_ORDER.forEach((positionClass) => {
      const image = `scm-marker-${positionClass}`;
      if (!this.map.hasImage(image)) {
        this.map.addImage(
          image,
          makeMarkerImage(MARKER_SHAPE[positionClass], COLOR[positionClass], MARKER_SIZE[positionClass]),
          { pixelRatio: 2 },
        );
      }

      const id = `scm-positions-${positionClass}`;
      this.map.addLayer({
        id,
        type: 'symbol',
        source: SOURCE_ID,
        filter: ['all', IS_POSITION, ['==', ['get', 'position_class'], positionClass]],
        layout: {
          'icon-image': image,
          // Markers are never dropped for want of room: a hidden container is
          // worse than a crowded map, and the count is the answer to crowding.
          'icon-allow-overlap': true,
          'icon-ignore-placement': true,
          // The count sits inside the marker; one container shows none, because
          // "1" on every dot is noise.
          'text-field': ['case', ['>', ['get', 'container_count'], 1], ['to-string', ['get', 'container_count']], ''],
          'text-size': 11,
          'text-allow-overlap': true,
          'text-ignore-placement': true,
        },
        paint: { 'text-color': MARKER_TEXT_COLOR },
      });
      this.map.on('click', id, (event) => this.selectPosition(event));
      this.pointer(id);
    });

    this.map.addLayer({
      id: 'scm-position-labels',
      type: 'symbol',
      source: SOURCE_ID,
      filter: IS_POSITION,
      layout: {
        'text-field': ['get', 'location_name'],
        'text-size': 11,
        'text-offset': [0, 1.8],
        'text-anchor': 'top',
        // Labels may be dropped where they would collide. The markers underneath
        // never are, so nothing is hidden — only a name is, until you zoom in.
        'text-allow-overlap': false,
      },
      paint: { 'text-color': '#334155', 'text-halo-color': '#ffffff', 'text-halo-width': 1.5 },
    });
  }

  addJourneyLayers() {
    // Solid: places the carrier confirmed, joined in order. Dashed: what is still
    // forecast. Neither is a vessel track, and the popup says so.
    this.map.addLayer({
      id: 'scm-line-actual',
      type: 'line',
      source: SOURCE_ID,
      filter: ['all', ['==', ['geometry-type'], 'LineString'], ['!', ['get', 'is_forecast']]],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': COLOR.actual, 'line-width': 3, 'line-opacity': 0.9 },
    });
    this.map.addLayer({
      id: 'scm-line-forecast',
      type: 'line',
      source: SOURCE_ID,
      filter: ['all', ['==', ['geometry-type'], 'LineString'], ['get', 'is_forecast']],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': COLOR.forecast,
        'line-width': 3,
        'line-opacity': 0.9,
        'line-dasharray': [1.5, 1.5],
      },
    });
    // A halo on the event the domain says the container is at now. Which event that
    // is comes from the server as is_current — it is not always the newest one, and
    // deciding it here would give the platform two answers to the same question.
    // The server drops the flag entirely once a canonical marker is being drawn,
    // so this halo and that marker can never both claim "now".
    this.map.addLayer({
      id: 'scm-event-current',
      type: 'circle',
      source: SOURCE_ID,
      filter: ['all', IS_EVENT, ['==', ['get', 'is_current'], true]],
      paint: {
        'circle-color': 'rgba(0,0,0,0)',
        'circle-radius': 13,
        'circle-stroke-width': 2,
        'circle-stroke-color': COLOR.actual,
        'circle-stroke-opacity': 0.5,
      },
    });
    this.map.addLayer({
      id: 'scm-events',
      type: 'circle',
      source: SOURCE_ID,
      // Events only. The canonical position markers share this source and are
      // drawn by scm-positions; a geometry-type filter would catch both and style
      // a physical location as a carrier event.
      filter: IS_EVENT,
      paint: {
        'circle-color': ['case', ['get', 'is_actual'], COLOR.actual, '#ffffff'],
        'circle-radius': 7,
        'circle-stroke-width': 2.5,
        'circle-stroke-color': ['case', ['get', 'is_actual'], '#ffffff', COLOR.forecast],
      },
    });
    this.map.addLayer({
      id: 'scm-event-selected',
      type: 'circle',
      source: SOURCE_ID,
      filter: ['all', IS_EVENT, ['==', ['get', 'event_id'], -1]],
      paint: {
        'circle-color': 'rgba(0,0,0,0)',
        'circle-radius': 14,
        'circle-stroke-width': 3,
        'circle-stroke-color': COLOR.selected,
      },
    });
    this.map.on('click', 'scm-events', (event) => this.selectEvent(event));
    this.pointer('scm-events');
  }

  pointer(layerId) {
    this.map.on('mouseenter', layerId, () => { this.map.getCanvas().style.cursor = 'pointer'; });
    this.map.on('mouseleave', layerId, () => { this.map.getCanvas().style.cursor = ''; });
  }

  // -- data ---------------------------------------------------------------

  refresh(url) {
    if (!url) return Promise.resolve();
    this.dataUrl = url;
    return fetch(url, { headers: { Accept: 'application/json' } })
      .then((response) => (response.ok ? response.json() : EMPTY))
      .then((data) => this.setData(data))
      .catch(() => this.setData(EMPTY));
  }

  setData(data) {
    this.data = data && data.features ? data : EMPTY;
    const source = this.map.getSource(SOURCE_ID);
    if (source) source.setData(this.data);
    this.element.classList.toggle('scm-map--empty', this.data.features.length === 0);
    this.fitToData();
  }

  fitToData() {
    const bounds = new mapboxgl.LngLatBounds();
    let count = 0;
    this.data.features.forEach((feature) => {
      const { type, coordinates } = feature.geometry;
      if (type === 'Point') {
        bounds.extend(coordinates);
        count += 1;
      } else if (type === 'LineString') {
        coordinates.forEach((point) => { bounds.extend(point); count += 1; });
      }
    });
    if (count === 0) return;
    this.map.fitBounds(bounds, { padding: FIT_PADDING, maxZoom: MAX_FIT_ZOOM, duration: 0 });
  }

  // -- interaction --------------------------------------------------------

  setBaseUrl(url) {
    this.baseUrl = url || this.baseUrl;
  }

  applyFilters() {
    return this.refresh(withMapFilters(this.baseUrl));
  }

  selectPosition(event) {
    const feature = event.features[0];
    this.openPositionPopup(feature);
    this.loadPanel(feature.properties.panel_url);
  }

  loadPanel(url) {
    const target = this.panelSelector ? document.querySelector(this.panelSelector) : null;
    if (!target || !url) return;
    // Rendered by Django, so the carrier's own strings are escaped server-side.
    if (window.htmx) {
      window.htmx.ajax('GET', url, { target, swap: 'innerHTML' });
    } else {
      fetch(url)
        .then((response) => response.text())
        .then((html) => { target.innerHTML = html; });
    }
  }

  openPositionPopup(feature) {
    const p = feature.properties;
    const node = document.createElement('div');
    node.className = 'scm-map-popup text-sm';

    // The kind of claim comes first. A reader who sees the place name first has
    // already assumed the container is there.
    node.appendChild(line(p.position_class_label, 'text-xs font-semibold uppercase tracking-wide opacity-70'));
    // "At Oceanterminalen", never a coordinate — the point locates the place.
    node.appendChild(line(p.place_statement || p.location_name, 'font-medium'));
    if (p.container_count > 1) {
      node.appendChild(line(`${p.container_count} containers`, 'text-xs'));
    } else if (p.container_number) {
      node.appendChild(line(p.container_number, 'text-xs font-mono'));
    }
    if (p.detail || p.source_label) {
      node.appendChild(line([p.detail, p.source_label].filter(Boolean).join(' · '), 'text-xs opacity-70'));
    }
    // Freshness, already worded by Django so it matches every other age on the page.
    if (p.age_display) node.appendChild(line(p.age_display, 'text-xs opacity-60'));
    if (p.occurred_at_display) node.appendChild(line(p.occurred_at_display, 'text-xs opacity-50'));
    if (p.arrival_state_label) node.appendChild(line(p.arrival_state_label, 'badge badge-xs badge-ghost mt-1'));
    if (p.overdue_count > 0) {
      node.appendChild(line(`${p.overdue_count} overdue`, 'badge badge-xs badge-warning mt-1'));
    }
    if (p.eta_display) node.appendChild(line(`ETA ${p.eta_display}`, 'text-xs opacity-60'));
    // Only on a current marker: repeating it on a destination marker would print
    // the same place twice and read as a route.
    if (p.is_current && p.destination_label) {
      node.appendChild(line(`→ ${p.destination_label}`, 'text-xs opacity-60'));
    }

    new mapboxgl.Popup({ closeButton: true, maxWidth: '280px' })
      .setLngLat(feature.geometry.coordinates)
      .setDOMContent(node)
      .addTo(this.map);
  }

  selectEvent(event) {
    const feature = event.features[0];
    this.highlightEvent(feature.properties.event_id);
    this.openEventPopup(feature);
    document.dispatchEvent(new CustomEvent('scm-map:event-selected', {
      detail: { eventId: feature.properties.event_id },
    }));
  }

  openEventPopup(feature) {
    const p = feature.properties;
    const node = document.createElement('div');
    node.className = 'scm-map-popup text-sm';
    // Built with textContent throughout: carrier wording is data, never markup.
    node.appendChild(line(p.event_title, 'font-medium'));
    if (!p.is_actual && p.event_time_type) node.appendChild(line(labelFor(p.event_time_type), 'badge badge-xs badge-ghost'));
    if (p.carrier_reference) node.appendChild(line(p.carrier_reference, 'text-xs opacity-60 font-mono'));
    if (p.position_label) node.appendChild(line(p.position_label, 'text-xs'));
    if (p.position_type_label) node.appendChild(line(p.position_type_label, 'text-xs opacity-60'));
    if (p.event_vessel_name) node.appendChild(line(p.event_vessel_name, 'text-xs opacity-70'));
    if (p.occurred_at_display) node.appendChild(line(p.occurred_at_display, 'text-xs opacity-60'));
    // Who reported this, including any second source that reported the same event.
    // Already joined and localised server-side; this only prints it.
    if (p.source_label) node.appendChild(line(p.source_label, 'text-xs opacity-60'));

    new mapboxgl.Popup({ closeButton: true, maxWidth: '260px' })
      .setLngLat(feature.geometry.coordinates)
      .setDOMContent(node)
      .addTo(this.map);
  }

  highlightEvent(eventId) {
    this.selectedEventId = eventId;
    if (this.map.getLayer('scm-event-selected')) {
      this.map.setFilter('scm-event-selected', ['all', IS_EVENT, ['==', ['get', 'event_id'], eventId]]);
    }
  }

  focusEvent(eventId) {
    const feature = this.data.features.find(
      (candidate) => candidate.geometry.type === 'Point' && String(candidate.properties.event_id) === String(eventId),
    );
    if (!feature) return false;
    this.highlightEvent(feature.properties.event_id);
    this.map.flyTo({ center: feature.geometry.coordinates, zoom: Math.max(this.map.getZoom(), 5), speed: 1.2 });
    this.openEventPopup(feature);
    return true;
  }
}

function line(text, className) {
  const element = document.createElement('div');
  element.className = className;
  element.textContent = text;
  return element;
}

function labelFor(timeType) {
  return timeType.charAt(0).toUpperCase() + timeType.slice(1);
}

// ---------------------------------------------------------------------------
// Markers
// ---------------------------------------------------------------------------

// A drawn shape rather than a font glyph or a bundled sprite. Glyph coverage
// varies by Mapbox style, and a marker that silently fails to render is worse than
// no marker at all — so the three shapes are painted here, where they cannot go
// missing.
function makeMarkerImage(shape, color, sizeInPixels) {
  const size = sizeInPixels * 2;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext('2d');
  const middle = size / 2;
  const radius = middle - 5;

  context.lineWidth = 5;
  context.strokeStyle = shape === 'disc' ? '#ffffff' : color;
  // A filled disc for the accepted position; hollow shapes for the weaker claims,
  // so the strongest marker is also the most solid-looking one.
  context.fillStyle = shape === 'disc' ? color : '#ffffff';

  context.beginPath();
  if (shape === 'disc') {
    context.arc(middle, middle, radius, 0, Math.PI * 2);
  } else if (shape === 'diamond') {
    context.moveTo(middle, middle - radius);
    context.lineTo(middle + radius, middle);
    context.lineTo(middle, middle + radius);
    context.lineTo(middle - radius, middle);
    context.closePath();
  } else {
    const side = radius * 1.6;
    context.rect(middle - side / 2, middle - side / 2, side, side);
  }
  context.fill();
  context.stroke();

  return { width: size, height: size, data: context.getImageData(0, 0, size, size).data };
}

// ---------------------------------------------------------------------------
// Map filters
//
// The map's own view state: which position classes to draw, and whether to show
// the destination overlay. Applied by asking the server again rather than by
// hiding layers, so the endpoint's answer and the map always agree about what is
// being shown — and so the destination overlay is genuinely absent until asked
// for, rather than delivered and hidden.
// ---------------------------------------------------------------------------

function mapFilterParams() {
  const params = new URLSearchParams();
  document.querySelectorAll('[data-scm-map-filter]').forEach((element) => {
    if (element.type === 'checkbox' && !element.checked) return;
    if (element.value) params.append(element.name, element.value);
  });
  return params;
}

function withMapFilters(base) {
  if (!base) return base;
  const params = mapFilterParams().toString();
  if (!params) return base;
  return `${base}${base.includes('?') ? '&' : '?'}${params}`;
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

let instance = null;

function initialise() {
  // Only pages that actually show a map get a Map object — never the base
  // template, and never twice for the same element.
  const element = document.querySelector('[data-scm-map][data-mapbox-token]');
  if (!element || element.dataset.scmMapReady === '1') return;
  element.dataset.scmMapReady = '1';
  instance = new VisibilityMap(element);
}

function onMapFilterChange(event) {
  if (!instance || !event.target.closest('[data-scm-map-filter]')) return;
  instance.applyFilters();
}

function onTimelineClick(event) {
  const trigger = event.target.closest('[data-map-event-id]');
  if (!trigger || !instance) return;
  if (instance.focusEvent(trigger.dataset.mapEventId)) event.preventDefault();
}

function onEventSelected(event) {
  // The timeline stays the authoritative chronology; the map only points at it.
  document.querySelectorAll('[data-map-event-id]').forEach((element) => {
    element.classList.toggle('scm-timeline-selected', element.dataset.mapEventId === String(event.detail.eventId));
  });
  const selected = document.querySelector(`[data-map-event-id="${event.detail.eventId}"]`);
  if (selected) selected.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function onAfterSwap(event) {
  // A board filter change swaps the board and leaves the map alone: we only point
  // its existing source at the URL describing the new selection, then re-apply the
  // map's own toggles on top — those live in the map card, outside the swap, and
  // must survive it.
  const source = event.target.querySelector
    ? event.target.querySelector('[data-scm-map-source]') || event.target.closest('[data-scm-map-source]')
    : null;
  if (instance && source) {
    instance.setBaseUrl(source.dataset.scmMapSource);
    instance.applyFilters();
  }
  initialise();
}

document.addEventListener('DOMContentLoaded', initialise);
document.addEventListener('click', onTimelineClick);
document.addEventListener('change', onMapFilterChange);
document.addEventListener('scm-map:event-selected', onEventSelected);
// htmx events bubble to document, so one listener covers every swap on the page.
document.addEventListener('htmx:afterSwap', onAfterSwap);

export { VisibilityMap };
