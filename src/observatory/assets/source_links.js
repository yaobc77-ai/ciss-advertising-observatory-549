var dagcomponentfuncs = window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {};
dagcomponentfuncs.SourceLink = function (props) {
    if (!props.enabled) return React.createElement('span', {className: 'source-muted'}, 'Links disabled');
    var valid = function (value) {
        try { var url = new URL(value); return (url.protocol === 'https:' || url.protocol === 'http:') ? url.href : null; }
        catch (error) { return null; }
    };
    var links = [];
    var original = valid(props.value);
    if (original) links.push(React.createElement('a', {key: 'original', href: original, target: '_blank', rel: 'noopener noreferrer'}, 'Original ↗'));
    return React.createElement('span', {className: 'source-cell'}, links.length ? links : React.createElement('span', {className: 'source-muted'}, 'Unavailable'));
};

dagcomponentfuncs.RecordLink = function (props) {
    var id = props.data && props.data.record_id;
    return id ? React.createElement('a', {href: '/records/' + encodeURIComponent(id), target: '_blank', rel: 'noopener noreferrer'}, 'View record ↗') : React.createElement('span', null, 'Unavailable');
};
dagcomponentfuncs.RecordTitle = function (props) {
    var id = props.data && props.data.record_id;
    var title = props.value || 'Untitled record';
    return id ? React.createElement('a', {
        href: '/records/' + encodeURIComponent(id), target: '_blank', rel: 'noopener noreferrer',
        className: 'record-title-link'
    }, title) : React.createElement('span', null, title);
};
dagcomponentfuncs.ArchiveLink = function (props) {
    if (!props.enabled) return React.createElement('span', {className:'source-muted'}, 'Links disabled');
    var data = props.data || {};
    if (data.snapshot_count > 0 && data.record_id) return React.createElement('a', {href:'/records/' + encodeURIComponent(data.record_id),target:'_blank',rel:'noopener noreferrer'}, 'Local PDF ↗');
    try {
        var url = new URL(data.archive_url);
        if ((url.protocol === 'https:' || url.protocol === 'http:') && !url.username && !url.password) return React.createElement('a',{href:url.href,target:'_blank',rel:'noopener noreferrer'},'Online archive ↗');
    } catch (error) { /* No valid archive destination. */ }
    return React.createElement('span',{className:'source-muted',title:props.value || 'No verified archived copy linked'},'Not linked');
};
