export type Source = "live";
export type Action = "pause" | "resume" | "restart";

export type Config = {
  search_limit: number;
  search_threshold: number;
};

export type Health = { reachable: boolean; ready: boolean };

export type PhotoProgress = {
  photo_id: number;
  image_id: number | null;
  image_url: string | null;
  back_url: string | null;
  title: string | null;
  stages: { key: string; status: string; reused: boolean; started_at: string | null }[];
};

export type JobSnapshot = {
  id: number;
  status: "queued" | "running" | "pausing" | "paused" | "restarting" | "completed" | "completed_with_errors";
  collection_progress: {
    completed_records: number;
    total_records: number | null;
    percent: number | null;
  };
  access_blocked: boolean;
  failed_records: number;
  error_code: string | null;
  activity: string | null;
  current: PhotoProgress | null;
  last_completed: (PhotoProgress & { save_task_id: number }) | null;
};

export type ImageResult = {
  image_id: number;
  photo_id: number;
  title: string;
  catalog_description: string;
  vision_description: string | null;
  subjects: string[];
  metadata: Record<string, unknown>;
  provenance: Record<string, unknown>;
  image_url: string | null;
  back_url: string | null;
  source_url: string | null;
  source: Source;
  relative_score: number;
  ranks: Record<string, number>;
  fused_score: number;
};

export type ChatResult = {
  text: string;
  images: ImageResult[];
  citations: { photo_id: number; title: string; url: string }[];
  requirements: string[];
  search_query: string | null;
  semantic_available: boolean;
  notices: string[];
  conversation_id: string | null;
  action: string;
  research?: {
    photo_id: number | null;
    status: string;
    candidates: {
      cluster_id: string | null;
      name: string;
      birth_year: string | null;
      places: string[];
      fate: string | null;
      basis: "source_linked" | "possible" | "user_selected";
      comparisons: Record<string, string>[];
    }[];
    total: number | null;
    reviewed: number;
    has_more: boolean;
  } | null;
  research_citations?: { source_id: string; title: string; url: string }[];
};

