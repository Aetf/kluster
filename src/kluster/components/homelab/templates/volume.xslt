<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="xml" encoding="UTF-8" omit-xml-declaration="yes"/>
  <xsl:template match="node()|@*">
    <xsl:copy><xsl:apply-templates select="node()|@*"/></xsl:copy>
  </xsl:template>
  <xsl:template match="/volume/target">
    <xsl:copy>
      <xsl:apply-templates select="node()[not(self::permissions)]|@*"/>
      <permissions>
        <xsl:apply-templates select="permissions/node()[not(self::mode)]|permissions/@*"/>
        <mode>0600</mode>
      </permissions>
    </xsl:copy>
  </xsl:template>
</xsl:stylesheet>
