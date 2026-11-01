/* Config layout comes from the pinned upstream header, never a Rust replica.
 * No inference implementation lives here; the caller loads the native library.
 */
#include "vendor/c-api.h"

typedef const SherpaOnnxKeywordSpotter *(*CreateKws)(
    const SherpaOnnxKeywordSpotterConfig *);

const SherpaOnnxKeywordSpotter *jarvis_create_kws(
    CreateKws create, const char *encoder, const char *decoder,
    const char *joiner, const char *tokens, const char *keywords,
    float score, float threshold) {
  SherpaOnnxKeywordSpotterConfig config = {0};
  config.feat_config.sample_rate = 16000;
  config.feat_config.feature_dim = 80;
  config.model_config.transducer.encoder = encoder;
  config.model_config.transducer.decoder = decoder;
  config.model_config.transducer.joiner = joiner;
  config.model_config.tokens = tokens;
  config.model_config.provider = "cpu";
  config.model_config.num_threads = 1;
  config.max_active_paths = 4;
  config.num_trailing_blanks = 1;
  config.keywords_score = score;
  config.keywords_threshold = threshold;
  config.keywords_file = keywords;
  return create(&config);
}
