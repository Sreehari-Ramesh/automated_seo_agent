<?php
/**
 * Plugin Name: SEO Agent Meta
 * Description: Registers custom AI SEO meta fields for REST API access and injects them into <head>.
 * Version:     1.0.0
 * Author:      SEO Agent
 *
 * Drop this file into /wp-content/mu-plugins/seo-agent-meta.php
 * OR activate it as a normal plugin from /wp-content/plugins/seo-agent-meta/seo-agent-meta.php
 */

if ( ! defined( 'ABSPATH' ) ) exit;

// ── 1. Register custom meta fields ──────────────────────────────────────────

add_action( 'init', function () {
    $fields = [
        'ai_meta_title'       => 'string',
        'ai_meta_description' => 'string',
        'ai_canonical_url'    => 'string',
        'ai_og_title'         => 'string',
        'ai_og_description'   => 'string',
        'ai_og_image'         => 'string',
        'ai_schema_json'      => 'string',
        'ai_focus_keyword'    => 'string',
    ];

    foreach ( [ 'post', 'page' ] as $post_type ) {
        foreach ( $fields as $key => $type ) {
            register_post_meta( $post_type, $key, [
                'show_in_rest'  => true,
                'single'        => true,
                'type'          => $type,
                'auth_callback' => function () {
                    return current_user_can( 'edit_posts' );
                },
            ] );
        }
    }
} );

// ── 2. Inject meta into <head> ───────────────────────────────────────────────

add_action( 'wp_head', function () {
    if ( ! is_singular() ) return;

    $post_id = get_the_ID();
    if ( ! $post_id ) return;

    $ai_title    = get_post_meta( $post_id, 'ai_meta_title',       true );
    $ai_desc     = get_post_meta( $post_id, 'ai_meta_description', true );
    $ai_canon    = get_post_meta( $post_id, 'ai_canonical_url',    true );
    $ai_og_title = get_post_meta( $post_id, 'ai_og_title',         true );
    $ai_og_desc  = get_post_meta( $post_id, 'ai_og_description',   true );
    $ai_og_image = get_post_meta( $post_id, 'ai_og_image',         true );
    $ai_schema   = get_post_meta( $post_id, 'ai_schema_json',      true );

    // Fallbacks if AI hasn't written values yet
    $title   = $ai_title  ?: get_the_title() . ' - ' . get_bloginfo( 'name' );
    $desc    = $ai_desc   ?: wp_trim_words( get_the_excerpt(), 30, '...' );
    $canon   = $ai_canon  ?: get_permalink( $post_id );
    $og_t    = $ai_og_title  ?: $title;
    $og_d    = $ai_og_desc   ?: $desc;
    $og_img  = $ai_og_image  ?: get_the_post_thumbnail_url( $post_id, 'large' );

    echo "\n<!-- SEO Agent -->\n";

    // Title
    echo '<title>' . esc_html( $title ) . "</title>\n";

    // Meta description
    if ( $desc ) {
        echo '<meta name="description" content="' . esc_attr( $desc ) . "\">\n";
    }

    // Canonical
    echo '<link rel="canonical" href="' . esc_url( $canon ) . "\">\n";

    // Open Graph
    echo '<meta property="og:title" content="' . esc_attr( $og_t ) . "\">\n";
    if ( $og_d ) {
        echo '<meta property="og:description" content="' . esc_attr( $og_d ) . "\">\n";
    }
    echo '<meta property="og:url" content="' . esc_url( $canon ) . "\">\n";
    echo '<meta property="og:type" content="article">' . "\n";
    if ( $og_img ) {
        echo '<meta property="og:image" content="' . esc_url( $og_img ) . "\">\n";
    }

    // Twitter meta tags
    // echo '<meta name="twitter:card" content="summary_large_image">' . "\n";
    // echo '<meta name="twitter:title" content="' . esc_attr( $og_t ) . "\">\n";
    // echo '<meta name="twitter:description" content="' . esc_attr( $og_d ) . "\">\n";

    // if ( $og_img ) {
    //     echo '<meta name="twitter:image" content="' . esc_url( $og_img ) . "\">\n";
    // }


    // JSON-LD schema
    if ( $ai_schema ) {
        // Validate it's real JSON before echoing
        $decoded = json_decode( $ai_schema );
        if ( $decoded !== null ) {
            echo '<script type="application/ld+json">' . wp_json_encode( $decoded, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE ) . "</script>\n";
        }
    }

    echo "<!-- /SEO Agent -->\n";
}, 1 ); // priority 1 = runs before Yoast / RankMath

// ── 3. Suppress Yoast <head> output when AI meta is present ─────────────────
// Only suppresses per-page — Yoast sitemap and other features still work.

add_action( 'template_redirect', function () {
    if ( ! is_singular() ) return;

    $post_id  = get_the_ID();
    $ai_title = $post_id ? get_post_meta( $post_id, 'ai_meta_title', true ) : '';

    if ( $ai_title && defined( 'WPSEO_VERSION' ) ) {
        // Remove Yoast head output for this page only
        $wpseo_front = YoastSEO()->classes->get( \Yoast\WP\SEO\Integrations\Front_End_Integration::class );
        remove_action( 'wpseo_head', [ $wpseo_front, 'present_head' ], -9999 );
    }
} );

add_action('init', function() {
    remove_action('wp_head', '_wp_render_title_tag', 1);
});

// ── Fix duplicate <title> issue ────────────────────────────────────────────
// Remove WordPress default title rendering so we control it fully

add_action('init', function () {
    remove_action('wp_head', '_wp_render_title_tag', 1);
});