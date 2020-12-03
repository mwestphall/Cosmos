from retrieval.retriever import Retriever
from elasticsearch_dsl import Search, Q
from elasticsearch_dsl.connections import connections
from elasticsearch import RequestsHttpConnection
from elasticsearch_dsl import Document, Text, connections, Integer, Float, Keyword, Join
from elasticsearch.helpers import bulk
import pandas as pd
import hashlib
import logging

logging.basicConfig(format='%(levelname)s :: %(asctime)s :: %(message)s', level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class EntityObjectIndex(Document):
    """
    We need to put entities and the objects associated on the same shard, so we are going have them inherit
    from this index class
    """
    entity_object = Join(relations={"entity": "object"})

    @classmethod
    def _matches(cls, hit):
        return False

    class Index:
        name = 'eo-site'
        settings = {
            'number_of_shards': 1,
            'number_of_replicas': 0
        }

class Entity(EntityObjectIndex):
    canonical_id = Text(fields={'raw': Keyword()})
    name = Text(fields={'raw': Keyword()})
    description = Text()
    types = Keyword(multi=True)
    aliases = Text(multi=True)
    dataset_id = Text(fields={'raw': Keyword()})

    @classmethod
    def _matches(cls, hit):
        """ Use Entity class for parent documents """
        return hit["_source"]["entity_object"] == "entity"

    @classmethod
    def search(cls, **kwargs):
        return cls._index.search(**kwargs).filter("term", entity_object="entity")

    def get_id(self):
        '''
        Elasticsearch ingest process would be greatly improved by having a unique ID per object.
        TODO: is this actually unique and deterministic?
        '''
        return hashlib.sha1(f"{self.canonical_id}{self.name}{self.description}{self.types}{self.aliases}{self.dataset_id}".encode('utf-8')).hexdigest()

    def add_object(self, cls, dataset_id, content, header_content, area, detect_score, postprocess_score, pdf_name, img_path=None, commit=True):
        obj = Object(
            # required make sure the answer is stored in the same shard
            _routing=self.meta.id,
            # since we don't have explicit index, ensure same index as self
            _index=self.meta.index,
            # set up the parent/child mapping
            entity_object={"name": "object", "parent": self.meta.id},
            # pass in the field values
            cls=cls,
            dataset_id=dataset_id,
            content=content,
            header_content=header_content,
            area=area,
            detect_score=detect_score,
            postprocess_score=postprocess_score,
            pdf_name=pdf_name,
            img_path=img_path
        )
        if commit:
            obj.save()
        return obj

    def search_objects(self):
        # search only our index
        s = Object.search()
        # filter for answers belonging to us
        s = s.filter("parent_id", type="object", id=self.meta.id)
        # add routing to only go to specific shard
        s = s.params(routing=self.meta.id)
        return s

    def get_objects(self):
        """
        Get objects either from inner_hits already present or by searching
        elasticsearch.
        """
        if "inner_hits" in self.meta and "object" in self.meta.inner_hits:
            return self.meta.inner_hits.object.hits
        return list(self.search_objects())

    def save(self, **kwargs):
        self.entity_object = "entity"
        return super(Entity, self).save(**kwargs)


class Object(EntityObjectIndex):
    cls = Text(fields={'raw': Keyword()})
    detect_score = Float()
    postprocess_score = Float()
    dataset_id = Text(fields={'raw': Keyword()})
    header_content = Text()
    content = Text()
    area = Integer()
    pdf_name = Text(fields={'raw': Keyword()})
    img_pth = Text(fields={'raw': Keyword()})

    def get_id(self):
        '''
        Elasticsearch ingest process would be greatly improved by having a unique ID per object.
        TODO: is this actually unique and deterministic?
        '''
        return hashlib.sha1(f"{self.cls}{self.detect_score}{self.postprocess_score}{self.dataset_id}{self.header_content}{self.content}{self.pdf_name}".encode('utf-8')).hexdigest()

    @classmethod
    def _matches(cls, hit):
        """ Use Object class for child documents with child name 'object' """
        return (
                isinstance(hit["_source"]["entity_object"], dict)
                and hit["_source"]["entity_object"].get("name") == "object"
        )

    @classmethod
    def search(cls, **kwargs):
        return cls._index.search(**kwargs).exclude("term", entity_object="entity")

    def save(self, **kwargs):
        # set routing to parents id automatically
        self.meta.routing = self.entity_object.parent
        return super(Object, self).save(**kwargs)


class FullDocument(Document):
    dataset_id = Text(fields={'raw': Keyword()})
    content = Text()
    name = Text(fields={'raw': Keyword()})

    class Index:
        name = 'fulldocument'
        settings = {
            'number_of_shards': 1,
            'number_of_replicas': 0
        }


class ElasticRetriever(Retriever):
    def __init__(self, hosts=['localhost'], awsauth=None):
        self.hosts = hosts
        self.awsauth = awsauth

    def search(self, query, entity_search=False, ndocs=30, page=0, cls=None, detect_min=None, postprocess_min=None):
        if self.awsauth is not None:
            connections.create_connection(hosts=self.hosts,
                                          http_auth=self.awsauth,
                                          use_ssl=True,
                                          verify_certs=True,
                                          connection_class=RequestsHttpConnection
                                          )
        else:
            connections.create_connection(hosts=self.hosts, timeout=20)
        if entity_search:
            es = Entity.search()
            q = Q('match', name=query)
            response = es.query(q).execute()
            logger.info('Done finding entity')
            for hit in response:
                s = hit.search_objects()
                if cls is not None:
                    s = s.filter('term', cls__raw=cls)
                if detect_min is not None:
                    s = s.filter('range', detect_score={'gte': detect_min})
                if postprocess_min is not None:
                    s = s.filter('range', postprocess_score={'gte': postprocess_min})
                start = page * ndocs
                end = start + ndocs
                contexts = []
                cq = Q('match', content=query)
                os_response = s.query(cq)[start:end].execute()
                print(os_response)
                for context in os_response:
                    contexts.append({'id': context.meta.id, 'pdf_name': context['pdf_name'], 'content': context['content']})
                logger.info(f'Found {len(contexts)} contexts')
                return {'id': hit.meta.id, 'entity': hit.name, 'entity_description': hit.description, 'entity_types': hit.types, 'contexts': contexts}
            return None
        else:
            q = Q('match', content=query)
            start = page * ndocs
            end = start + ndocs
            s = Search(index='fulldocument').query(q)[start:end]
            response = s.execute()
            logger.error('Done finding docs')
            contexts = []
            context_set = set()
            for result in response:
                s = Object.search()
                s = s.filter('term', pdf_name__raw=result['name'])
                if cls is not None:
                    s = s.filter('term', cls__raw=cls)
                if detect_min is not None:
                    s = s.filter('range', detect_score={'gte': detect_min})
                if postprocess_min is not None:
                    s = s.filter('range', postprocess_score={'gte': postprocess_min})

                for context in s.scan():
                    if context['content'] in context_set: # Skip dupes
                        continue
                    context_set.add(context['content'])
                    contexts.append({'id': context.meta.id, 'pdf_name': context['pdf_name'], 'content': context['content']})
            logger.error(f'Found {len(contexts)} contexts')
            return contexts

    def get_object(self, id):
        if self.awsauth is not None:
            connections.create_connection(hosts=self.hosts,
                                          http_auth=self.awsauth,
                                          use_ssl=True,
                                          verify_certs=True,
                                          connection_class=RequestsHttpConnection
                                          )
        else:
            connections.create_connection(hosts=self.hosts)
        return Object.get(id=id)

    def build_index(self, document_parquet, entities_parquet, section_parquet, tables_parquet, figures_parquet, equations_parquet):
        if self.awsauth is not None:
            connections.create_connection(hosts=self.hosts,
                                          http_auth=self.awsauth,
                                          use_ssl=True,
                                          verify_certs=True,
                                          connection_class=RequestsHttpConnection
                                          )
        else:
            connections.create_connection(hosts=self.hosts)
        logger.info('Building elastic index')
        connections.create_connection(hosts=self.hosts)
        index_template = EntityObjectIndex._index.as_template("base")
        index_template.save()
        FullDocument.init()
        # This is a parquet file to load from
        df = pd.read_parquet(document_parquet)
        for ind, row in df.iterrows():
            FullDocument(name=row['pdf_name'], dataset_id=row['dataset_id'], content=row['content']).save()
        logger.info('Done building document index')
        df = pd.read_parquet(entities_parquet)
        for ind, row in df.iterrows():
            Entity(canonical_id=row['id'],
                   name=row['name'],
                   description=row['description'],
                   types=row['types'].tolist(),
                   aliases=row['aliases'].tolist(),
                   dataset_id=row['dataset_id']).save()
        logger.info('Done building entities index')

        df = pd.read_parquet(section_parquet)
        for ind, row in df.iterrows():
            entities = row['ents_linked']
            to_add = []
            for entity in entities:
                es = Entity.search()
                es = es.filter('term', canonical_id__raw=entity)
                response = es.execute()
                for hit in response:
                    to_add.append(hit.add_object('Section',
                                   row['dataset_id'],
                                   row['content'],
                                   row['section_header'],
                                   50,
                                   row['detect_score'],
                                   row['postprocess_score'],
                                   row['pdf_name'],
                                   commit=False))
                    if len(to_add) == 100:
                        bulk(connections.get_connection(), (o.to_dict(True) for o in to_add), request_timeout=20, max_retries=1)
                        to_add = []
            if to_add == []: continue
            bulk(connections.get_connection(), (o.to_dict(True) for o in to_add), request_timeout=20, max_retries=1)
            to_add = []
        logger.info('Done building section index')

        if tables_parquet != '':
            df = pd.read_parquet(tables_parquet)
            to_add = []
            for ind, row in df.iterrows():
                entities = row['ents_linked']
                for entity in entities:
                    es = Entity.search()
                    es = es.filter('term', canonical_id__raw=entity)
                    response = es.execute()
                    for hit in response:
                        to_add.append(hit.add_object('Table',
                                       row['dataset_id'],
                                       row['content'],
                                       row['caption_content'],
                                       50,
                                       row['detect_score'],
                                       row['postprocess_score'],
                                       row['pdf_name'],
                                       row['img_pth'],
                                       commit=False))
                        if len(to_add) == 100:
                            bulk(connections.get_connection(), (o.to_dict(True) for o in to_add), request_timeout=20, max_retries=1)
                            to_add = []
                if to_add == []: continue
                bulk(connections.get_connection(), (o.to_dict(True) for o in to_add), request_timeout=20, max_retries=1)
                to_add = []
            logger.info('Done building tables index')
        if figures_parquet != '':
            df = pd.read_parquet(figures_parquet)
            to_add = []
            for ind, row in df.iterrows():
                entities = row['ents_linked']
                for entity in entities:
                    es = Entity.search()
                    es = es.filter('term', canonical_id__raw=entity)
                    response = es.execute()
                    for hit in response:
                        to_add.append(hit.add_object('Figure',
                                       row['dataset_id'],
                                       row['content'],
                                       row['caption_content'],
                                       50,
                                       row['detect_score'],
                                       row['postprocess_score'],
                                       row['pdf_name'],
                                       row['img_pth'],
                                       commit=False))
                        if len(to_add) == 100:
                            bulk(connections.get_connection(), (o.to_dict(True) for o in to_add), request_timeout=20, max_retries=1)
                            to_add = []
                if to_add == []: continue
                bulk(connections.get_connection(), (o.to_dict(True) for o in to_add), request_timeout=20, max_retries=1)
            to_add = []
            logger.info('Done building figures index')

        if equations_parquet != '':
            df = pd.read_parquet(equations_parquet)
            to_add = []
                entities = row['ents_linked']
                for entity in entities:
                    es = Entity.search()
                    es = es.filter('term', canonical_id__raw=entity)
                    response = es.execute()
                    for hit in response:
                        to_add.append(hit.add_object('Equation',
                                       row['dataset_id'],
                                       row['content'],
                                       None,
                                       50,
                                       row['detect_score'],
                                       row['postprocess_score'],
                                       row['pdf_name'],
                                       row['img_pth'],
                                       commit=False))
                        if len(to_add) == 100:
                            bulk(connections.get_connection(), (o.to_dict(True) for o in to_add), request_timeout=20, max_retries=1)
                            to_add = []
                if to_add == []: continue
                bulk(connections.get_connection(), (o.to_dict(True) for o in to_add), request_timeout=20, max_retries=1)
            to_add = []
            logger.info('Done building equations index')

        logger.info('Done building object index')

    def delete(self, dataset_id):
        if self.awsauth is not None:
            connections.create_connection(hosts=self.hosts,
                                          http_auth=self.awsauth,
                                          use_ssl=True,
                                          verify_certs=True,
                                          connection_class=RequestsHttpConnection
                                          )
        else:
            connections.create_connection(hosts=self.hosts)
        s = Search(index='fulldocument')
        q = Q()
        q = q & Q('match', dataset_id__raw=dataset_id)
        result = s.query(q).delete()
        logger.info(result)
        s = Search(index='eo-site')
        q = Q()
        q = q & Q('match', dataset_id__raw=dataset_id)
        result = s.query(q).delete()
        logger.info(result)

    def rerank(self, query, contexts):
        raise NotImplementedError('ElasticRetriever does not rerank results')