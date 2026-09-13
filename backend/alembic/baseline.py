import sqlalchemy as sa


def baseline_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table(
        'otp_challenges',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('phone', sa.String(length=20), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('requested_role', sa.String(length=16), nullable=False),
        sa.Column('requested_name', sa.String(length=80), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_otp_challenges_phone',
        metadata.tables['otp_challenges'].c['phone'],
        unique=False,
    )
    sa.Table(
        'users',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('phone', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_users_phone',
        metadata.tables['users'].c['phone'],
        unique=True,
    )
    sa.Index(
        'ix_users_role',
        metadata.tables['users'].c['role'],
        unique=False,
    )
    sa.Table(
        'baselines',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('parent_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('metric', sa.String(length=32), nullable=False),
        sa.Column('time_slot', sa.String(length=32), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('sample_count', sa.Integer(), nullable=False),
        sa.Column('required_count', sa.Integer(), nullable=False),
        sa.Column('median', sa.Float(), nullable=True),
        sa.Column('mad', sa.Float(), nullable=True),
        sa.Column('window_from', sa.Date(), nullable=False),
        sa.Column('window_to', sa.Date(), nullable=False),
        sa.Column('anchor_set_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_baselines_metric',
        metadata.tables['baselines'].c['metric'],
        unique=False,
    )
    sa.Index(
        'ix_baselines_parent_id',
        metadata.tables['baselines'].c['parent_id'],
        unique=False,
    )
    sa.Table(
        'calls',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('parent_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('child_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('state', sa.String(length=32), nullable=False),
        sa.Column('room_name', sa.String(length=120), nullable=False),
        sa.Column('recording_enabled', sa.Boolean(), nullable=False),
        sa.Column('recording_disabled_reason', sa.String(length=32), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('time_slot', sa.String(length=32), nullable=True),
        sa.Column('duration_sec', sa.Integer(), nullable=True),
        sa.Column('parent_speech_sec', sa.Integer(), nullable=True),
        sa.Column('asked_question_ids', sa.JSON(), nullable=False),
        sa.Column('raw_audio_purged_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('processing_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_calls_child_id',
        metadata.tables['calls'].c['child_id'],
        unique=False,
    )
    sa.Index(
        'ix_calls_parent_id',
        metadata.tables['calls'].c['parent_id'],
        unique=False,
    )
    sa.Index(
        'ix_calls_room_name',
        metadata.tables['calls'].c['room_name'],
        unique=True,
    )
    sa.Index(
        'ix_calls_state',
        metadata.tables['calls'].c['state'],
        unique=False,
    )
    sa.Table(
        'consent_records',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('document_version', sa.String(length=40), nullable=False),
        sa.Column('decision', sa.String(length=16), nullable=False),
        sa.Column('agreed_items', sa.JSON(), nullable=False),
        sa.Column('agreed_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_consent_records_user_id',
        metadata.tables['consent_records'].c['user_id'],
        unique=False,
    )
    sa.Table(
        'devices',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('platform', sa.String(length=16), nullable=False),
        sa.Column('token', sa.Text(), nullable=False),
        sa.Column('voip_token', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_devices_user_id',
        metadata.tables['devices'].c['user_id'],
        unique=False,
    )
    sa.Table(
        'families',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('created_by', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_families_created_by',
        metadata.tables['families'].c['created_by'],
        unique=False,
    )
    sa.Table(
        'parent_profiles',
        metadata,
        sa.Column(
            'parent_id', sa.String(length=36), sa.ForeignKey('users.id'),
            nullable=False, primary_key=True,
        ),
        sa.Column('conditions', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Table(
        'reports',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('parent_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('period', sa.String(length=16), nullable=False),
        sa.Column('from_date', sa.Date(), nullable=False),
        sa.Column('to_date', sa.Date(), nullable=False),
        sa.Column('state', sa.String(length=32), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_reports_parent_id',
        metadata.tables['reports'].c['parent_id'],
        unique=False,
    )
    sa.Table(
        'acoustic_analysis_runs',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('call_id', sa.String(length=36), sa.ForeignKey('calls.id'), nullable=False),
        sa.Column('analyzer_version', sa.String(length=48), nullable=False),
        sa.Column('cough_detector_version', sa.String(length=48), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_acoustic_analysis_runs_call_id',
        metadata.tables['acoustic_analysis_runs'].c['call_id'],
        unique=True,
    )
    sa.Table(
        'acoustic_features',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('call_id', sa.String(length=36), sa.ForeignKey('calls.id'), nullable=False),
        sa.Column('audio_source', sa.String(length=24), nullable=False),
        sa.Column('metric', sa.String(length=32), nullable=False),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('unit', sa.String(length=24), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('unmeasurable_reason', sa.String(length=48), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_acoustic_features_call_id',
        metadata.tables['acoustic_features'].c['call_id'],
        unique=False,
    )
    sa.Index(
        'ix_acoustic_features_metric',
        metadata.tables['acoustic_features'].c['metric'],
        unique=False,
    )
    sa.Table(
        'audio_assets',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('call_id', sa.String(length=36), sa.ForeignKey('calls.id'), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('uri', sa.Text(), nullable=False),
        sa.Column('content_type', sa.String(length=80), nullable=False),
        sa.Column('duration_sec', sa.Float(), nullable=True),
        sa.Column('sample_rate', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('egress_id', sa.String(length=80), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('purged_at', sa.DateTime(timezone=True), nullable=True),
    )
    sa.Index(
        'ix_audio_assets_call_id',
        metadata.tables['audio_assets'].c['call_id'],
        unique=False,
    )
    sa.Index(
        'ix_audio_assets_egress_id',
        metadata.tables['audio_assets'].c['egress_id'],
        unique=False,
    )
    sa.Index(
        'ix_audio_assets_kind',
        metadata.tables['audio_assets'].c['kind'],
        unique=False,
    )
    sa.Table(
        'change_signals',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('parent_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('call_id', sa.String(length=36), sa.ForeignKey('calls.id'), nullable=False),
        sa.Column('metric', sa.String(length=32), nullable=False),
        sa.Column('time_slot', sa.String(length=32), nullable=False),
        sa.Column('vs_anchor', sa.JSON(), nullable=True),
        sa.Column('vs_rolling', sa.JSON(), nullable=True),
        sa.Column('consecutive_weeks', sa.Integer(), nullable=False),
        sa.Column('promoted', sa.Boolean(), nullable=False),
        sa.Column('acute', sa.Boolean(), nullable=False),
        sa.Column('summary_text', sa.Text(), nullable=True),
        sa.Column('acute_text', sa.Text(), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_change_signals_call_id',
        metadata.tables['change_signals'].c['call_id'],
        unique=False,
    )
    sa.Index(
        'ix_change_signals_parent_id',
        metadata.tables['change_signals'].c['parent_id'],
        unique=False,
    )
    sa.Table(
        'extraction_evidence',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('call_id', sa.String(length=36), sa.ForeignKey('calls.id'), nullable=False),
        sa.Column('facts', sa.JSON(), nullable=False),
        sa.Column('schema_version', sa.String(length=24), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_extraction_evidence_call_id',
        metadata.tables['extraction_evidence'].c['call_id'],
        unique=True,
    )
    sa.Table(
        'family_members',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('family_id', sa.String(length=36), sa.ForeignKey('families.id'), nullable=False),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('relation', sa.String(length=24), nullable=False),
        sa.Column('invited_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_family_members_family_id',
        metadata.tables['family_members'].c['family_id'],
        unique=False,
    )
    sa.Index(
        'ix_family_members_user_id',
        metadata.tables['family_members'].c['user_id'],
        unique=False,
    )
    sa.Table(
        'health_extractions',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('call_id', sa.String(length=36), sa.ForeignKey('calls.id'), nullable=False),
        sa.Column('parse_status', sa.String(length=16), nullable=False),
        sa.Column('symptom', sa.Text(), nullable=True),
        sa.Column('medication', sa.Text(), nullable=True),
        sa.Column('activity', sa.Text(), nullable=True),
        sa.Column('sleep', sa.Text(), nullable=True),
        sa.Column('raw_transcript', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_health_extractions_call_id',
        metadata.tables['health_extractions'].c['call_id'],
        unique=True,
    )
    sa.Table(
        'repeat_events',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('call_id', sa.String(length=36), sa.ForeignKey('calls.id'), nullable=False),
        sa.Column('speaker', sa.String(length=16), nullable=False),
        sa.Column('start_ms', sa.Integer(), nullable=False),
        sa.Column('end_ms', sa.Integer(), nullable=False),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('matched_text', sa.Text(), nullable=False),
        sa.Column('rule_id', sa.String(length=64), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('rule_version', sa.String(length=24), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_repeat_events_call_id',
        metadata.tables['repeat_events'].c['call_id'],
        unique=False,
    )
    sa.Table(
        'transcripts',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column('call_id', sa.String(length=36), sa.ForeignKey('calls.id'), nullable=False),
        sa.Column('provider', sa.String(length=80), nullable=False),
        sa.Column('excluded', sa.Boolean(), nullable=False),
        sa.Column('exclusion_reason', sa.String(length=48), nullable=True),
        sa.Column('parent_speech_sec', sa.Integer(), nullable=False),
        sa.Column('segments', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_transcripts_call_id',
        metadata.tables['transcripts'].c['call_id'],
        unique=True,
    )
    sa.Table(
        'invitations',
        metadata,
        sa.Column('id', sa.String(length=36), nullable=False, primary_key=True),
        sa.Column(
            'member_id', sa.String(length=36), sa.ForeignKey('family_members.id'), nullable=False,
        ),
        sa.Column('code', sa.String(length=6), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    sa.Index(
        'ix_invitations_code',
        metadata.tables['invitations'].c['code'],
        unique=True,
    )
    sa.Index(
        'ix_invitations_member_id',
        metadata.tables['invitations'].c['member_id'],
        unique=False,
    )
    return metadata
