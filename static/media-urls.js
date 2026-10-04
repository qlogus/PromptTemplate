class MediaUrls {
    constructor() { this.urls = []; }
    url(source) {
        if (!source.startsWith('data:')) return source;
        const comma = source.indexOf(',');
        const metadata = source.slice(5, comma);
        const bytes = Uint8Array.from(atob(source.slice(comma + 1)), char => char.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bytes], {type:metadata.split(';')[0]}));
        this.urls.push(url);
        return url;
    }
    dispose() {
        this.urls.forEach(url => URL.revokeObjectURL(url));
        this.urls = [];
    }
}
const exampleMedia = new MediaUrls();
