<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="xml" encoding="UTF-8" omit-xml-declaration="yes"/>
  <xsl:template match="node()|@*">
    <xsl:copy><xsl:apply-templates select="node()|@*"/></xsl:copy>
  </xsl:template>
  <xsl:template match="/domain/devices/disk[@device='disk']">
    <xsl:copy>
      <xsl:apply-templates select="@*"/>
      <driver name="qemu" type="raw" cache="none" discard="unmap"/>
      <xsl:apply-templates select="node()[not(self::driver)]"/>
    </xsl:copy>
  </xsl:template>
</xsl:stylesheet>
