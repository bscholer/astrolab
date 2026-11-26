<!-- Small line icons for the editor. 24x24 grid, stroked in currentColor. -->
<script lang="ts">
  let { name, size = 20 }: { name: string; size?: number } = $props();

  const PATHS: Record<string, string> = {
    sun: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4',
    moon: 'M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z',
    contrast: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 3v18',
    highlights: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 3a9 9 0 0 1 0 18M7 8h3M6 12h4M7 16h3',
    shadows: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 3a9 9 0 0 0 0 18M14 8h3M14 12h4M14 16h3',
    gradient: 'M4 4h16v16H4zM4 9h16M4 14h16M8 4v16',
    balance: 'M12 3v18M5 7h14M5 7l-3 7a3 3 0 0 0 6 0zM19 7l-3 7a3 3 0 0 0 6 0z',
    hue: 'M12 3a9 9 0 1 0 0 18c1.5 0 2-1 2-2s-1-1.5-1-2.5 1-1.5 2-1.5h2a4 4 0 0 0 4-4c0-4.4-4-8-9-8zM7.5 11.5h.01M10 7.5h.01M15 7.5h.01',
    drop: 'M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z',
    leaf: 'M5 19c0-8 5-13 14-14-1 9-6 14-14 14zM5 19l7-7',
    star: 'M12 3l2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.6 6.6 19.5l1.2-6-4.5-4.2 6.1-.7z',
    sparkle: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z',
    focus: 'M3 8V5a2 2 0 0 1 2-2h3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 2h-3M8 21H5a2 2 0 0 1-2-2v-3M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
    wave: 'M2 12h2M6 8v8M10 5v14M14 8v8M18 10v4M22 12h-2',
    looks: 'M5 19L15 9M14 4l1 2 2 1-2 1-1 2-1-2-2-1 2-1zM19 11l.7 1.3L21 13l-1.3.7L19 15l-.7-1.3L17 13l1.3-.7z',
    crop: 'M6 2v14a2 2 0 0 0 2 2h14M2 6h14a2 2 0 0 1 2 2v14',
    light: 'M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM12 1v2M12 21v2M1 12h2M21 12h2',
    color: 'M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z',
    detail: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z',
    versions: 'M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 3',
    undo: 'M9 14L4 9l5-5M4 9h10.5a5.5 5.5 0 0 1 0 11H11',
    redo: 'M15 14l5-5-5-5M20 9H9.5a5.5 5.5 0 0 0 0 11H13',
    back: 'M15 18l-6-6 6-6',
    reset: 'M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5',
    more: 'M5 12h.01M12 12h.01M19 12h.01',
    download: 'M12 3v12M7 10l5 5 5-5M5 21h14',
    split: 'M12 3v18M4 5h16v14H4z',
    check: 'M5 12l5 5L20 7',
  };
</script>

<svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor"
  stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <path d={PATHS[name] ?? ''} />
</svg>
