// Supply chain visibility map.
//
// One module for all three map contexts — the overview, a shipment journey and a
// container journey — because they draw the same GeoJSON contract and differ only
// in which layers they add.
//
// Two things this module deliberately does not do:
//
//   * It never decides what a position means. Quality, whether an event was
//     observed or forecast, and every label come from Container SCM as feature
//     properties. Re-deriving any of that here would give the platform two
//     answers to the same question.
//
//   * It never rebuilds the map. Filters replace the data in the existing
//     GeoJSON source, so panning and zoom survive a filter change.
//
// Mapbox is an enhancement. Without a token the page renders its configuration
// notice, this module finds no map element to initialise, and nothing throws.

import mapboxgl from 'mapbox-gl';
import 'mapbox-gl/dist/mapbox-gl.css';

const SOURCE_ID = 'scm-visibility';
const FALLBACK_CENTER = [10, 35];
const FALLBACK_ZOOM = 1.4;
const FIT_PADDING = 56;
const MAX_FIT_ZOOM = 9;

// Semantic colours, mirroring the roles DaisyUI uses for the same meanings.
// Fixed hex rather than theme variables because Mapbox GL cannot parse the
// oklch() values the theme exposes.
const COLOR = {
  onTime: '#16a34a',
  delayed: '#f59e0b',
  exception: '#dc2626',
  unknown: '#64748b',
  actual: '#0f766e',
  forecast: '#6366f1',
  selected: '#0284c7',
};

const HEALTH_COLOR = [
  'match',
  ['get', 'health'],
  'exception', COLOR.exception,
  'delayed', COLOR.delayed,
  'on_time', COLOR.onTime,
  COLOR.unknown,
];

const EMPTY = { type: 'FeatureCollection', features: [] };

class VisibilityMap {
  constructor(element) {
    this.element = element;
    this.mode = element.dataset.mapMode || 'overview';
    this.dataUrl = element.dataset.mapDataUrl || '';
    this.panelSelector = element.dataset.mapPanelTarget || '';
    this.clustered = this.mode === 'overview';
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
      // Clustering only on the overview: a journey has few points and they must
      // each stay visible, while the overview has to scale to a whole fleet.
      cluster: this.clustered,
      clusterRadius: 44,
      clusterMaxZoom: 8,
    });
    if (this.clustered) {
      this.addClusterLayers();
      this.addObjectLayers();
    } else {
      this.addJourneyLayers();
    }
    this.map.resize();
    this.refresh(this.dataUrl);
  }

  addClusterLayers() {
    this.map.addLayer({
      id: 'scm-clusters',
      type: 'circle',
      source: SOURCE_ID,
      filter: ['has', 'point_count'],
      paint: {
        'circle-color': COLOR.unknown,
        'circle-opacity': 0.85,
        'circle-radius': ['step', ['get', 'point_count'], 16, 10, 22, 50, 30],
        'circle-stroke-width': 2,
        'circle-stroke-color': '#ffffff',
      },
    });
    this.map.addLayer({
      id: 'scm-cluster-count',
      type: 'symbol',
      source: SOURCE_ID,
      filter: ['has', 'point_count'],
      layout: { 'text-field': ['get', 'point_count_abbreviated'], 'text-size': 12 },
      paint: { 'text-color': '#ffffff' },
    });
    this.map.on('click', 'scm-clusters', (event) => this.zoomIntoCluster(event));
    this.pointer('scm-clusters');
  }

  addObjectLayers() {
    this.map.addLayer({
      id: 'scm-objects',
      type: 'circle',
      source: SOURCE_ID,
      filter: ['!', ['has', 'point_count']],
      paint: {
        'circle-color': HEALTH_COLOR,
        'circle-radius': ['case', ['>', ['get', 'container_count'], 1], 12, 8],
        'circle-stroke-width': 2,
        'circle-stroke-color': '#ffffff',
      },
    });
    this.map.addLayer({
      id: 'scm-object-labels',
      type: 'symbol',
      source: SOURCE_ID,
      filter: ['!', ['has', 'point_count']],
      layout: {
        'text-field': ['get', 'label'],
        'text-size': 11,
        'text-offset': [0, 1.4],
        'text-anchor': 'top',
        'text-allow-overlap': false,
      },
      paint: { 'text-color': '#334155', 'text-halo-color': '#ffffff', 'text-halo-width': 1.5 },
    });
    this.map.on('click', 'scm-objects', (event) => this.selectObject(event));
    this.pointer('scm-objects');
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
    this.map.addLayer({
      id: 'scm-events',
      type: 'circle',
      source: SOURCE_ID,
      filter: ['==', ['geometry-type'], 'Point'],
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
      filter: ['==', ['get', 'event_id'], -1],
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

  zoomIntoCluster(event) {
    const feature = event.features[0];
    this.map.getSource(SOURCE_ID).getClusterExpansionZoom(feature.properties.cluster_id, (error, zoom) => {
      if (error) return;
      this.map.easeTo({ center: feature.geometry.coordinates, zoom });
    });
  }

  selectObject(event) {
    const properties = event.features[0].properties;
    const target = this.panelSelector ? document.querySelector(this.panelSelector) : null;
    if (!target || !properties.panel_url) return;
    // Rendered by Django, so the carrier's own strings are escaped server-side.
    if (window.htmx) {
      window.htmx.ajax('GET', properties.panel_url, { target, swap: 'innerHTML' });
    } else {
      fetch(properties.panel_url)
        .then((response) => response.text())
        .then((html) => { target.innerHTML = html; });
    }
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

    new mapboxgl.Popup({ closeButton: true, maxWidth: '260px' })
      .setLngLat(feature.geometry.coordinates)
      .setDOMContent(node)
      .addTo(this.map);
  }

  highlightEvent(eventId) {
    this.selectedEventId = eventId;
    if (this.map.getLayer('scm-event-selected')) {
      this.map.setFilter('scm-event-selected', ['==', ['get', 'event_id'], eventId]);
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
  // A filter change swaps the board and leaves the map alone: we only point its
  // existing source at the URL describing the new selection.
  const source = event.target.querySelector
    ? event.target.querySelector('[data-scm-map-source]') || event.target.closest('[data-scm-map-source]')
    : null;
  if (instance && source) instance.refresh(source.dataset.scmMapSource);
  initialise();
}

document.addEventListener('DOMContentLoaded', initialise);
document.addEventListener('click', onTimelineClick);
document.addEventListener('scm-map:event-selected', onEventSelected);
// htmx events bubble to document, so one listener covers every swap on the page.
document.addEventListener('htmx:afterSwap', onAfterSwap);

export { VisibilityMap };
